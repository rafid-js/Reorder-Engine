"""
Per-SKU return rate calculation and early warning detection.

Computes for each SKU:
  return_rate_30d = flagged_30d / gross_orders_30d
  return_rate_7d  = flagged_7d  / gross_orders_7d

  No return data (new product or no sales): 0% — computed from real data only.

Early Warning Signal:
  Triggered when: return_rate_7d - return_rate_30d > RETURN_EARLY_WARNING_THRESHOLD (10pp)
  Example: 30d rate = 22%, 7d rate = 38% → FLAGGED
  Signals a possible quality issue, sizing problem, or supplier batch defect.

Hold Flag:
  return_rate_30d > RETURN_HOLD_THRESHOLD (50%)
  → Output must carry human review flag; reorder qty shown but blocked from auto-action.

High Risk Flag:
  return_rate_30d > RETURN_HIGH_RISK_THRESHOLD (40%)
  → Orange cell in Sheets, noted in email.

This module must run BEFORE engine/velocity.py so that velocity uses
per-SKU return rates (not the old global RETURN_RATE).
"""

from datetime import datetime, timedelta, timezone

import config
from config import logger


def compute_return_rates(
    merged_skus: list[dict],
    flagged_data: dict[str, dict[str, int]],
) -> list[dict]:
    """
    Enrich each merged SKU dict with per-SKU return rate metrics and flags.

    Args:
        merged_skus:   output of data/merger.merge()
        flagged_data:  output of data/returns.pull_flagged()

    Adds to each SKU dict:
        flagged_30d:           int   — units flagged/returned in 30d
        flagged_7d:            int   — units flagged/returned in last 7d
        gross_orders_30d:      int   — total WC orders in 30d window
        gross_orders_7d:       int   — estimated WC orders in last 7d
        return_rate_30d:       float — 30d rate (0–1); fallback 0.35 if no data
        return_rate_7d:        float — 7d rate (0–1); fallback = return_rate_30d
        return_early_warning:  bool  — True if 7d rate spikes >10pp over 30d
        high_return_risk:      bool  — True if return_rate_30d > 40%
        hold_for_review:       bool  — True if return_rate_30d > 50%
    """
    now_utc = datetime.now(timezone.utc)
    cutoff_7d = now_utc - timedelta(days=7)

    enriched: list[dict] = []
    early_warning_count = 0
    hold_count = 0

    for sku_data in merged_skus:
        sku = sku_data["sku"]
        order_dates: list[datetime] = sku_data.get("wc_order_dates", [])
        total_ordered: int = sku_data.get("total_ordered", 0)

        # ── Gross orders ──────────────────────────────────────────────────────
        gross_orders_30d = total_ordered  # full lookback window from WooCommerce

        # 7-day gross orders: scale line-item count by avg qty per line
        lines_7d = sum(1 for d in order_dates if d is not None and d >= cutoff_7d)
        avg_qty = (total_ordered / len(order_dates)) if order_dates else 0.0
        gross_orders_7d = round(lines_7d * avg_qty)

        # ── Flagged counts ────────────────────────────────────────────────────
        flagged = flagged_data.get(sku, {})
        flagged_30d: int = flagged.get("flagged_30d", 0)
        flagged_7d: int = flagged.get("flagged_7d", 0)
        has_return_data = sku in flagged_data

        # ── Return rate 30d ───────────────────────────────────────────────────
        if has_return_data and gross_orders_30d > 0:
            return_rate_30d = min(flagged_30d / gross_orders_30d, 1.0)
        elif gross_orders_30d == 0:
            # No sales → no returns possible
            return_rate_30d = 0.0
        else:
            # Has sales but no return history yet → use 15% industry baseline
            return_rate_30d = config.RETURN_RATE_FALLBACK

        # ── Return rate 7d ────────────────────────────────────────────────────
        if gross_orders_7d > 0 and flagged_7d > 0:
            return_rate_7d = min(flagged_7d / gross_orders_7d, 1.0)
        else:
            # No 7d data → assume same as 30d (no spike; no false warning)
            return_rate_7d = return_rate_30d

        # ── Flags ─────────────────────────────────────────────────────────────
        spike = return_rate_7d - return_rate_30d
        return_early_warning = spike > config.RETURN_EARLY_WARNING_THRESHOLD
        high_return_risk = return_rate_30d > config.RETURN_HIGH_RISK_THRESHOLD
        hold_for_review = return_rate_30d > config.RETURN_HOLD_THRESHOLD

        if return_early_warning:
            early_warning_count += 1
        if hold_for_review:
            hold_count += 1

        enriched.append(
            {
                **sku_data,
                "flagged_30d": flagged_30d,
                "flagged_7d": flagged_7d,
                "gross_orders_30d": gross_orders_30d,
                "gross_orders_7d": gross_orders_7d,
                "return_rate_30d": round(return_rate_30d, 4),
                "return_rate_7d": round(return_rate_7d, 4),
                "return_early_warning": return_early_warning,
                "high_return_risk": high_return_risk,
                "hold_for_review": hold_for_review,
            }
        )

    logger.info(
        "Return rates computed for %d SKUs — "
        "⚠️ Early warnings: %d | 🛑 Hold for review (>50%%): %d | "
        "🟠 High risk (>40%%): %d",
        len(enriched),
        early_warning_count,
        hold_count,
        sum(1 for s in enriched if s.get("high_return_risk")),
    )
    return enriched


def get_warning_skus(skus: list[dict]) -> list[dict]:
    """
    Return SKUs with active early warning signals, sorted by spike size descending.
    Spike = return_rate_7d - return_rate_30d.
    """
    warnings = [s for s in skus if s.get("return_early_warning")]
    return sorted(
        warnings,
        key=lambda s: s.get("return_rate_7d", 0) - s.get("return_rate_30d", 0),
        reverse=True,
    )
