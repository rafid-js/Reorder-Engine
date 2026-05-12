"""
Claude API intelligence layer.

Sends computed SKU data to claude-sonnet-4-20250514 and gets back per-SKU
actionable recommendations including trend analysis, reorder reasoning,
risk flags, and supplier notes.

If the Claude API call fails, returns None so callers can fall back to
raw-data-only reports.
"""

import json
import time
from typing import Any

import anthropic

import config
from config import logger

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

_SYSTEM_PROMPT = (
    "You are Winterfell's inventory intelligence engine for a fast-fashion brand "
    "in Bangladesh. Analyze the SKU data provided and give actionable reorder "
    "recommendations. Be direct, specific, and commercially aggressive. "
    "Never recommend underordering a trending product. "
    "All currency values are in BDT (Bangladeshi Taka). "
    "Respond with a valid JSON array only — no prose, no markdown fences."
)

_USER_PROMPT_TEMPLATE = """Analyze the following SKUs that need reorder attention.
For each SKU, output a JSON object with these exact keys:
  - sku: the SKU string
  - trend: "ACCELERATING" | "DECELERATING" | "STABLE"
  - trend_note: one sentence comparing 7-day vs 14-day velocity
  - recommended_reorder_qty: integer (your recommended order quantity, may differ from formula if you see strong trend signals)
  - reasoning: one to two sentences explaining why
  - risk_flag: string — note if Meta Ads spend is high (use "HIGH_ADS_SPEND" if meta_ads_high=true, else "NONE")
  - supplier_note: one sentence — if multiple suppliers exist mention preference, else "N/A"
  - action_note: one concise action line for the ops team (max 15 words)

Input data (JSON):
{sku_json}

Return ONLY a JSON array of objects matching the schema above. No extra text."""


def _prepare_payload(skus: list[dict]) -> list[dict[str, Any]]:
    """Slim down each SKU to what Claude needs — avoid sending huge blobs."""
    payload = []
    for s in skus:
        payload.append(
            {
                "sku": s["sku"],
                "product_name": s.get("product_name", ""),
                "current_stock": s.get("current_stock", 0),
                "daily_velocity_14d": s.get("daily_velocity_14d", 0.0),
                "daily_velocity_7d": s.get("daily_velocity_7d", 0.0),
                "days_remaining": s.get("days_remaining", 9999),
                "true_demand": s.get("true_demand", 0),
                "last_purchase_price_bdt": s.get("last_purchase_price", 0.0),
                "urgency_tier": s.get("urgency_tier", "HEALTHY"),
                "formula_reorder_qty": s.get("reorder_qty", 0),
                "supplier_name": s.get("supplier_name", ""),
                "meta_ads_high": False,  # placeholder — wire in when Meta Ads data is available
            }
        )
    return payload


def _parse_claude_response(text: str) -> list[dict] | None:
    """Extract the JSON array from Claude's response."""
    text = text.strip()
    # Strip markdown fences if present despite instructions
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        logger.warning("Claude returned JSON but not an array: %s", type(data))
        return None
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse Claude JSON response: %s", exc)
        return None


def get_recommendations(skus: list[dict]) -> list[dict] | None:
    """
    Call Claude API with the given SKU list and return per-SKU recommendations.

    Returns a list of recommendation dicts indexed by SKU, or None on failure.
    Falls back gracefully — callers must handle None.
    """
    if not skus:
        return []

    payload = _prepare_payload(skus)
    user_message = _USER_PROMPT_TEMPLATE.format(sku_json=json.dumps(payload, indent=2))

    last_exc: Exception | None = None

    for attempt in range(1, config.MAX_API_RETRIES + 1):
        try:
            logger.info(
                "Calling Claude API for %d SKUs (attempt %d)...", len(skus), attempt
            )
            message = _client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=4096,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            raw_text = message.content[0].text
            recs = _parse_claude_response(raw_text)
            if recs is not None:
                logger.info("Claude returned recommendations for %d SKUs.", len(recs))
                return recs
            # Parsing failed — retry
            last_exc = ValueError("Response parse failed")
        except anthropic.APIError as exc:
            last_exc = exc
            wait = config.RETRY_BACKOFF_BASE ** attempt
            logger.warning(
                "Claude API attempt %d failed: %s — retrying in %ds", attempt, exc, wait
            )
            time.sleep(wait)

    logger.error(
        "Claude API failed after %d retries. Proceeding without AI recommendations. Last error: %s",
        config.MAX_API_RETRIES, last_exc,
    )
    return None


def merge_recommendations(skus: list[dict], recs: list[dict] | None) -> list[dict]:
    """
    Attach Claude recommendations to each SKU dict.

    If recs is None (API failure), adds empty placeholder fields.
    """
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
        s["claude_recommended_qty"] = int(rec.get("recommended_reorder_qty") or s.get("reorder_qty", 0))
        s["claude_reasoning"] = rec.get("reasoning", "")
        s["claude_risk_flag"] = rec.get("risk_flag", "NONE")
        s["claude_supplier_note"] = rec.get("supplier_note", "")
        s["claude_action_note"] = rec.get("action_note", "")

    return skus
