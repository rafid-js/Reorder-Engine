"""
Claude API intelligence layer.

Three separate API calls:

1. get_recommendations(skus)
   Reorder recommendations for CRITICAL and WARNING SKUs.

2. get_return_analysis(warning_skus)
   Deep analysis for SKUs with a spiking return rate (early warning).
   Diagnoses: quality issue / sizing problem / supplier batch defect.
   Recommends: hold reorder / investigate supplier / ops action.

3. get_size_analysis(size_products)
   Per-parent-SKU size health analysis: identifies which sizes to scale up/down,
   flags stockout risks, and validates the suggested size ratio.

All calls fall back gracefully to empty placeholder data if the API call fails.
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


_BATCH_SIZE = 20          # SKUs per Claude call — 20 × ~250 tokens ≈ 5k output, fits in 8192
_BATCH_DELAY_SECONDS = 35 # Haiku: 10K output tokens/min; 5K per batch → safe at 35s gap


def _call_claude(system: str, user: str, label: str, model: str | None = None) -> list[dict] | None:
    """Single Claude API call with retry logic. Returns parsed list or None."""
    last_exc: Exception | None = None
    _model = model or config.CLAUDE_MODEL

    for attempt in range(1, config.MAX_API_RETRIES + 1):
        try:
            logger.info("Calling Claude API [%s] attempt %d (model: %s)...", label, attempt, _model)
            message = _client.messages.create(
                model=_model,
                max_tokens=8192,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            recs = _parse_claude_response(message.content[0].text)
            if recs is not None:
                logger.info("Claude [%s] returned %d items.", label, len(recs))
                return recs
            last_exc = ValueError("Response parse failed")
        except anthropic.RateLimitError as exc:
            last_exc = exc
            wait = 60 * attempt  # back off harder on rate limit
            logger.warning(
                "Claude API [%s] rate limited (attempt %d) — waiting %ds",
                label, attempt, wait,
            )
            time.sleep(wait)
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
    Batches into chunks of _BATCH_SIZE to stay under Claude's per-minute token limit.
    Returns merged list of dicts or None if every batch fails.
    """
    if not skus:
        return []

    payload = _prepare_reorder_payload(skus)
    batches = [payload[i:i + _BATCH_SIZE] for i in range(0, len(payload), _BATCH_SIZE)]
    total_batches = len(batches)

    all_results: list[dict] = []
    any_success = False

    for idx, batch in enumerate(batches, start=1):
        logger.info(
            "Claude reorder: batch %d/%d (%d SKUs)...", idx, total_batches, len(batch)
        )
        user_msg = _REORDER_PROMPT.format(sku_json=json.dumps(batch, indent=2))
        result = _call_claude(_REORDER_SYSTEM, user_msg, f"reorder-batch-{idx}", model=config.CLAUDE_BULK_MODEL)
        if result:
            all_results.extend(result)
            any_success = True
        else:
            logger.warning("Claude reorder batch %d/%d failed — skipping.", idx, total_batches)

        if idx < total_batches:
            logger.info(
                "Waiting %ds before next Claude batch to respect rate limit...",
                _BATCH_DELAY_SECONDS,
            )
            time.sleep(_BATCH_DELAY_SECONDS)

    return all_results if any_success else None


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


# ── Size ratio analysis ───────────────────────────────────────────────────────

_SIZE_SYSTEM = (
    "You are Winterfell's inventory intelligence engine for a fast-fashion brand "
    "in Bangladesh. Analyze size-variant performance data and recommend production "
    "ratio adjustments. Be specific about which sizes to scale up or down based on "
    "velocity data and sell-through rates. Consider stockout risk as the highest priority. "
    "Respond with a valid JSON array only — no prose, no markdown fences."
)

_SIZE_PROMPT = """Analyze the size-variant performance for these parent SKUs.
For each parent SKU, output a JSON object with these exact keys:
  - parent_sku: the parent SKU string
  - size_notes: dict of {{size: one-sentence note}} for any size with a notable flag (skip OK sizes)
  - ratio_verdict: "OPTIMIZE" if the suggested ratio significantly differs from current sell-through | "MAINTAIN" if current distribution looks healthy
  - top_action: one concise action for the production manager (max 20 words)
  - risk_summary: one sentence summarizing the biggest size-level risk for this product

Data context:
  - size_ratio_pct: share of this size in overall product velocity
  - sell_through_pct: units sold in last 14 days / (stock + units sold)
  - health_flag: 💀 SIZE_STOCKOUT (stock=0 or days_remaining=0) | 🔥 FAST_MOVER (>80% sell-through) | 🧊 SLOW_MOVER (<20%) | ⚠️ OVERSTOCK_RISK (>60 days stock) | OK
  - suggested_qty: ratio-normalized production recommendation
  - days_remaining: stock days left at current net velocity

Input data (JSON):
{size_json}

Return ONLY a JSON array. No extra text."""


def _prepare_size_payload(size_products: list[dict]) -> list[dict[str, Any]]:
    payload = []
    for parent in size_products:
        sizes_payload = []
        for s in parent["sizes"]:
            sizes_payload.append({
                "size": s["size"],
                "sku": s["sku"],
                "current_stock": s["current_stock"],
                "net_velocity_14d": s["net_velocity_14d"],
                "size_ratio_pct": s["size_ratio_pct"],
                "sell_through_pct": s["sell_through_pct"],
                "days_remaining": s["days_remaining"],
                "suggested_qty": s["suggested_qty"],
                "health_flag": s["health_flag"],
            })
        payload.append({
            "parent_sku": parent["parent_sku"],
            "product_name": parent["product_name"],
            "category": parent["category"],
            "total_reorder_qty": parent["total_reorder_qty"],
            "sizes": sizes_payload,
        })
    return payload


def get_size_analysis(size_products: list[dict]) -> list[dict] | None:
    """
    Size health analysis for all multi-size parent SKUs.
    Returns list of dicts keyed by parent_sku, or None on failure.
    """
    if not size_products:
        return []
    payload = _prepare_size_payload(size_products)
    user_msg = _SIZE_PROMPT.format(size_json=json.dumps(payload, indent=2))
    return _call_claude(_SIZE_SYSTEM, user_msg, "size-analysis")


def merge_size_analysis(size_products: list[dict], analysis: list[dict] | None) -> list[dict]:
    """
    Attach Claude size analysis to each parent SKU dict and its size rows.
    Adds: claude_ratio_verdict, claude_top_action, claude_risk_summary
    Per-size: claude_size_note (from size_notes dict)
    """
    if not analysis:
        for parent in size_products:
            parent.setdefault("claude_ratio_verdict", "N/A")
            parent.setdefault("claude_top_action", "")
            parent.setdefault("claude_risk_summary", "")
            for s in parent["sizes"]:
                s.setdefault("claude_size_note", "")
        return size_products

    analysis_map = {r["parent_sku"]: r for r in analysis}
    for parent in size_products:
        rec = analysis_map.get(parent["parent_sku"], {})
        parent["claude_ratio_verdict"] = rec.get("ratio_verdict", "N/A")
        parent["claude_top_action"] = rec.get("top_action", "")
        parent["claude_risk_summary"] = rec.get("risk_summary", "")
        size_notes = rec.get("size_notes", {})
        for s in parent["sizes"]:
            s["claude_size_note"] = size_notes.get(s["size"], "")
    return size_products


# ── Kill Chain exit strategy ──────────────────────────────────────────────────

_KILL_CHAIN_SYSTEM = (
    "You are Winterfell's inventory exit strategist for a premium Gen Z fast fashion brand "
    "in Bangladesh. For each dead stock SKU, recommend the fastest way to recover capital "
    "without destroying brand equity. Consider: markdown depth, bundle pairing with fast movers, "
    "clearance timing relative to upcoming drops, and whether the product can be reworked or "
    "repurposed. Winterfell is a premium Gen Z fast fashion brand — protect brand perception "
    "while clearing dead stock aggressively. "
    "Respond with a valid JSON array only — no prose, no markdown fences."
)

_KILL_CHAIN_PROMPT = """Analyze the following dead stock SKUs and recommend exit strategies.
For each SKU, output a JSON object with these exact keys:
  - sku: the SKU string
  - confirmed_stage: "WATCH" | "MARKDOWN" | "BUNDLE" | "LIQUIDATE" (confirm or upgrade our computed stage)
  - exit_action: specific action with timeline (e.g. "Run 25% flash sale for 5 days starting Friday")
  - brand_risk: "Low" | "Medium" | "High" — risk to brand perception from this action
  - ops_instruction: one-line instruction for ops team (max 20 words)
  - bundle_with: SKU of the best fast mover to pair with (only for BUNDLE stage, else empty string)

Data context:
  - dead_stock_score: 50–100 composite signal (higher = worse)
  - kill_chain_stage: our computed stage (you may upgrade but not downgrade)
  - days_since_last_sale: days since any unit of this SKU sold
  - sell_through_30d_pct: % of available inventory sold in last 30 days
  - stock_age_days: days since first unit entered the system
  - capital_locked: BDT value of stuck inventory
  - estimated_recovery: BDT at recommended exit path
  - fast_movers: top currently selling SKUs available for bundle pairing

Dead stock SKUs (JSON):
{dead_stock_json}

Fast movers available for bundle pairing:
{fast_movers_json}

Return ONLY a JSON array. No extra text."""


def _prepare_kill_chain_payload(dead_stock_skus: list[dict]) -> list[dict[str, Any]]:
    payload = []
    for s in dead_stock_skus:
        payload.append({
            "sku": s["sku"],
            "product_name": s.get("product_name", ""),
            "dead_stock_score": s.get("dead_stock_score", 0),
            "kill_chain_stage": s.get("kill_chain_stage", ""),
            "days_since_last_sale": s.get("days_since_last_sale", 0),
            "sell_through_30d_pct": s.get("sell_through_30d_pct", 0),
            "stock_age_days": s.get("stock_age_days", 0),
            "current_stock": s.get("current_stock", 0),
            "capital_locked_bdt": s.get("capital_locked", 0),
            "estimated_recovery_bdt": s.get("estimated_recovery", 0),
            "suggested_discount_pct": s.get("suggested_discount_pct", 0),
            "supplier_name": s.get("supplier_name", ""),
        })
    return payload


def get_kill_chain_analysis(
    dead_stock_skus: list[dict],
    fast_movers: list[dict] | None = None,
) -> list[dict] | None:
    """
    Exit strategy recommendations for all dead stock SKUs (score >= 50).
    fast_movers: top active SKUs passed for bundle pairing suggestions.
    Returns list of dicts or None on failure.
    """
    if not dead_stock_skus:
        return []

    fast_movers = fast_movers or []
    fast_mover_payload = [
        {
            "sku": s["sku"],
            "product_name": s.get("product_name", ""),
            "net_velocity_14d": s.get("net_velocity_14d", 0.0),
        }
        for s in sorted(fast_movers, key=lambda x: x.get("net_velocity_14d", 0), reverse=True)[:8]
    ]

    payload = _prepare_kill_chain_payload(dead_stock_skus)
    user_msg = _KILL_CHAIN_PROMPT.format(
        dead_stock_json=json.dumps(payload, indent=2),
        fast_movers_json=json.dumps(fast_mover_payload, indent=2),
    )
    return _call_claude(_KILL_CHAIN_SYSTEM, user_msg, "kill-chain")


def merge_kill_chain_analysis(
    dead_stock_skus: list[dict],
    analysis: list[dict] | None,
) -> list[dict]:
    """Attach Claude kill chain analysis to each dead stock SKU dict."""
    if not analysis:
        for s in dead_stock_skus:
            s.setdefault("claude_kill_chain_stage", s.get("kill_chain_stage", ""))
            s.setdefault("claude_exit_action", "Manual review required.")
            s.setdefault("claude_brand_risk", "Unknown")
            s.setdefault("claude_ops_instruction", "")
            s.setdefault("claude_bundle_with", "")
        return dead_stock_skus

    analysis_map = {r["sku"]: r for r in analysis}
    for s in dead_stock_skus:
        rec = analysis_map.get(s["sku"], {})
        s["claude_kill_chain_stage"] = rec.get("confirmed_stage", s.get("kill_chain_stage", ""))
        s["claude_exit_action"]      = rec.get("exit_action", "")
        s["claude_brand_risk"]       = rec.get("brand_risk", "Unknown")
        s["claude_ops_instruction"]  = rec.get("ops_instruction", "")
        s["claude_bundle_with"]      = rec.get("bundle_with", "")
    return dead_stock_skus
