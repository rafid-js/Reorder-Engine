"""
Claude API intelligence layer.

Two separate API calls:

1. get_recommendations(skus)
   Reorder recommendations for CRITICAL and WARNING SKUs.

2. get_return_analysis(warning_skus)
   Deep analysis for SKUs with a spiking return rate (early warning).
   Diagnoses: quality issue / sizing problem / supplier batch defect.
   Recommends: hold reorder / investigate supplier / ops action.

Both fall back gracefully to empty placeholder data if the API call fails.
"""

import json
import time
from typing import Any

import anthropic

import config
from config import logger

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# ── Shared helpers ────────────────────────────────────────────────────────────

def _parse_claude_response(text: str) -> list[dict] | None:
    """Extract the JSON array from Claude's response, stripping markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.startswith("```")).strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        logger.warning("Claude returned JSON but not an array: %s", type(data))
        return None
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse Claude JSON response: %s", exc)
        return None


def _call_claude(system: str, user: str, label: str) -> list[dict] | None:
    """Single Claude API call with retry logic. Returns parsed list or None."""
    last_exc: Exception | None = None

    for attempt in range(1, config.MAX_API_RETRIES + 1):
        try:
            logger.info("Calling Claude API [%s] attempt %d...", label, attempt)
            message = _client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            recs = _parse_claude_response(message.content[0].text)
            if recs is not None:
                logger.info("Claude [%s] returned %d items.", label, len(recs))
                return recs
            last_exc = ValueError("Response parse failed")
        except anthropic.APIError as exc:
            last_exc = exc
            wait = config.RETRY_BACKOFF_BASE ** attempt
            logger.warning(
                "Claude API [%s] attempt %d failed: %s — retrying in %ds",
                label, attempt, exc, wait,
            )
            time.sleep(wait)

    logger.error(
        "Claude API [%s] failed after %d retries: %s",
        label, config.MAX_API_RETRIES, last_exc,
    )
    return None


# ── Reorder recommendations ───────────────────────────────────────────────────

_REORDER_SYSTEM = (
    "You are Winterfell's inventory intelligence engine for a fast-fashion brand "
    "in Bangladesh. Analyze the SKU data and give actionable reorder recommendations. "
    "Be direct, specific, and commercially aggressive. "
    "Never recommend underordering a trending product. "
    "All currency values are in BDT (Bangladeshi Taka). "
    "net_velocity figures are already adjusted for a 15% cancellation rate "
    "and each SKU's individual return rate — they represent true net consumption. "
    "pre_order_qty is committed demand in Nuport on-hold (pre-orders); if high "
    "relative to effective_stock, urgency is higher than days_remaining alone suggests. "
    "Respond with a valid JSON array only — no prose, no markdown fences."
)

_REORDER_PROMPT = """Analyze the following SKUs that need reorder attention.
For each SKU, output a JSON object with these exact keys:
  - sku: the SKU string
  - trend: "ACCELERATING" | "DECELERATING" | "STABLE"
  - trend_note: one sentence comparing net 7-day vs 14-day velocity
  - recommended_reorder_qty: integer (be aggressive if accelerating or preorder_qty is high)
  - reasoning: one to two sentences — mention pre-orders if significant
  - risk_flag: "HIGH_ADS_SPEND" if meta_ads_high=true | "HIGH_PREORDER_PRESSURE" if preorder_qty > effective_stock | "NONE" otherwise
  - supplier_note: one sentence on supplier/lead time if known, else "N/A"
  - action_note: one concise action line for ops team (max 15 words)

Data context:
  - net_velocity_14d/7d: adjusted for cancel rate + per-SKU return rate
  - raw_velocity_14d: gross order volume (reference only)
  - return_rate_30d: this SKU's 30-day return rate (already baked into net velocity)
  - preorder_qty: Nuport on-hold — will draw down stock when dispatched
  - effective_stock: current_stock minus preorder_qty
  - days_remaining: effective_stock ÷ net_velocity_14d

Input data (JSON):
{sku_json}

Return ONLY a JSON array. No extra text."""


def _prepare_reorder_payload(skus: list[dict]) -> list[dict[str, Any]]:
    payload = []
    for s in skus:
        payload.append({
            "sku": s["sku"],
            "product_name": s.get("product_name", ""),
            "current_stock": s.get("current_stock", 0),
            "preorder_qty": s.get("preorder_qty", 0),
            "effective_stock": s.get("effective_stock", s.get("current_stock", 0)),
            "raw_velocity_14d": s.get("raw_velocity_14d", 0.0),
            "net_velocity_14d": s.get("net_velocity_14d", 0.0),
            "net_velocity_7d": s.get("net_velocity_7d", 0.0),
            "return_rate_30d": s.get("return_rate_30d", config.RETURN_RATE_FALLBACK),
            "days_remaining": s.get("days_remaining", 9999),
            "true_demand": s.get("true_demand", 0),
            "last_purchase_price_bdt": s.get("last_purchase_price", 0.0),
            "urgency_tier": s.get("urgency_tier", "HEALTHY"),
            "formula_reorder_qty": s.get("reorder_qty", 0),
            "supplier_name": s.get("supplier_name", ""),
            "meta_ads_high": False,  # placeholder — wire in when Meta Ads data available
        })
    return payload


def get_recommendations(skus: list[dict]) -> list[dict] | None:
    """
    Reorder recommendations for CRITICAL/WARNING SKUs.
    Returns list of dicts or None on failure.
    """
    if not skus:
        return []
    payload = _prepare_reorder_payload(skus)
    user_msg = _REORDER_PROMPT.format(sku_json=json.dumps(payload, indent=2))
    return _call_claude(_REORDER_SYSTEM, user_msg, "reorder")


def merge_recommendations(skus: list[dict], recs: list[dict] | None) -> list[dict]:
    """Attach Claude reorder recommendations to each SKU dict."""
    if not recs:
        for s in skus:
            s["claude_trend"] = "N/A"
            s["claude_trend_note"] = "AI recommendations unavailable."
            s["claude_recommended_qty"] = s.get("reorder_qty", 0)
            s["claude_reasoning"] = ""
            s["claude_risk_flag"] = "N/A"
            s["claude_supplier_note"] = ""
            s["claude_action_note"] = "Manual review required."
        return skus

    rec_map = {r["sku"]: r for r in recs}
    for s in skus:
        rec = rec_map.get(s["sku"], {})
        s["claude_trend"] = rec.get("trend", "STABLE")
        s["claude_trend_note"] = rec.get("trend_note", "")
        s["claude_recommended_qty"] = int(
            rec.get("recommended_reorder_qty") or s.get("reorder_qty", 0)
        )
        s["claude_reasoning"] = rec.get("reasoning", "")
        s["claude_risk_flag"] = rec.get("risk_flag", "NONE")
        s["claude_supplier_note"] = rec.get("supplier_note", "")
        s["claude_action_note"] = rec.get("action_note", "")
    return skus


# ── Return rate early warning analysis ───────────────────────────────────────

_RETURN_SYSTEM = (
    "You are Winterfell's inventory intelligence engine for a fast-fashion brand "
    "in Bangladesh. The following SKUs have a sharply rising return rate this week "
    "vs their 30-day average. For each, analyze whether this signals a quality issue, "
    "sizing problem, or supplier batch defect. Be specific and commercially direct. "
    "Respond with a valid JSON array only — no prose, no markdown fences."
)

_RETURN_PROMPT = """These SKUs have a return rate spike this week vs their 30-day baseline.
For each SKU, output a JSON object with these exact keys:
  - sku: the SKU string
  - diagnosis: "QUALITY_ISSUE" | "SIZING_PROBLEM" | "SUPPLIER_BATCH_DEFECT" | "UNKNOWN"
  - analysis: two sentences — what the spike pattern suggests about root cause
  - hold_reorder: true if you recommend pausing the reorder until investigated, else false
  - investigate_supplier: true if this warrants contacting the supplier, else false
  - action: one specific action for the ops team (max 20 words)

Data context:
  - return_rate_30d: baseline return rate over 30 days
  - return_rate_7d: return rate this week (the spike)
  - spike_pp: percentage point increase (7d minus 30d)
  - A spike >10pp is the trigger threshold

Input data (JSON):
{sku_json}

Return ONLY a JSON array. No extra text."""


def _prepare_return_payload(skus: list[dict]) -> list[dict[str, Any]]:
    payload = []
    for s in skus:
        rate_30d = s.get("return_rate_30d", 0.0)
        rate_7d = s.get("return_rate_7d", 0.0)
        payload.append({
            "sku": s["sku"],
            "product_name": s.get("product_name", ""),
            "return_rate_30d_pct": round(rate_30d * 100, 1),
            "return_rate_7d_pct": round(rate_7d * 100, 1),
            "spike_pp": round((rate_7d - rate_30d) * 100, 1),
            "flagged_30d": s.get("flagged_30d", 0),
            "flagged_7d": s.get("flagged_7d", 0),
            "gross_orders_30d": s.get("gross_orders_30d", 0),
            "supplier_name": s.get("supplier_name", ""),
            "hold_for_review": s.get("hold_for_review", False),
        })
    return payload


def get_return_analysis(warning_skus: list[dict]) -> list[dict] | None:
    """
    Deep return-rate analysis for SKUs with an early warning spike.
    Returns list of dicts or None on failure.
    """
    if not warning_skus:
        return []
    payload = _prepare_return_payload(warning_skus)
    user_msg = _RETURN_PROMPT.format(sku_json=json.dumps(payload, indent=2))
    return _call_claude(_RETURN_SYSTEM, user_msg, "return-analysis")


def merge_return_analysis(skus: list[dict], analysis: list[dict] | None) -> list[dict]:
    """
    Attach Claude return analysis to each SKU dict.
    Only SKUs with return_early_warning=True will have meaningful values.
    """
    if not analysis:
        for s in skus:
            s.setdefault("claude_return_diagnosis", "N/A")
            s.setdefault("claude_return_analysis", "")
            s.setdefault("claude_return_hold", s.get("hold_for_review", False))
            s.setdefault("claude_return_investigate", False)
            s.setdefault("claude_return_action", "Manual review required.")
        return skus

    analysis_map = {r["sku"]: r for r in analysis}
    for s in skus:
        rec = analysis_map.get(s["sku"], {})
        s["claude_return_diagnosis"] = rec.get("diagnosis", "UNKNOWN")
        s["claude_return_analysis"] = rec.get("analysis", "")
        s["claude_return_hold"] = bool(rec.get("hold_reorder", s.get("hold_for_review", False)))
        s["claude_return_investigate"] = bool(rec.get("investigate_supplier", False))
        s["claude_return_action"] = rec.get("action", "")
    return skus
