"""
Velocity calculations, per-SKU cancel/return buffering, and urgency tiering.

Pipeline dependency: engine/return_signals.compute_return_rates() must run
BEFORE this module so that each SKU dict already carries `return_rate_30d`.

Net velocity formula per SKU:
  net_velocity = raw_velocity × (1 - CANCEL_RATE) × (1 - return_rate_30d)

Where:
  CANCEL_RATE      = 0.15 (global — 15% of orders cancel before delivery)
  return_rate_30d  = per-SKU 30-day return rate from Nuport flagged data
                     (fallback 0.35 for new products with no history)

days_remaining uses effective_stock (current stock minus committed pre-orders)
so pre-orders that haven't dispatched yet are treated as already consumed.

Pre-orders (preorder_qty from Nuport on-hold) reduce effective stock:
  effective_stock = max(current_stock - preorder_qty, 0)
  days_remaining  = effective_stock / net_velocity_14d
"""

from datetime import datetime, timedelta, timezone

import config
from config import logger

_URGENCY_CRITICAL = "CRITICAL"
_URGENCY_WARNING = "WARNING"
_URGENCY_HEALTHY = "HEALTHY"

_VERY_HIGH_DAYS = 9999  # sentinel when velocity is zero


def compute_velocity(merged_skus: list[dict]) -> list[dict]:
    """
    Enrich each SKU dict with velocity metrics and urgency tier.

    Expects: return_rate_30d already present on each dict (from return_signals).

    Adds:
        raw_velocity_14d, raw_velocity_7d   — gross order volume ÷ days
        net_velocity_14d, net_velocity_7d   — after cancel + per-SKU return buffer
        daily_velocity_14d / 7d             — aliases to net values (for downstream compat)
        effective_stock                     — current_stock minus preorder_qty
        days_remaining                      — effective_stock / net_velocity_14d
        urgency_tier                        — CRITICAL / WARNING / HEALTHY
    """
    now_utc = datetime.now(timezone.utc)
    cutoff_14d = now_utc - timedelta(days=config.VELOCITY_LONG_DAYS)
    cutoff_7d = now_utc - timedelta(days=config.VELOCITY_SHORT_DAYS)

    enriched: list[dict] = []

    for sku_data in merged_skus:
        order_dates: list[datetime] = sku_data.get("wc_order_dates", [])
        total_ordered: int = sku_data.get("total_ordered", 0)
        current_stock: int = sku_data.get("current_stock", 0)
        preorder_qty: int = sku_data.get("preorder_qty", 0)

        # Per-SKU return rate — set by return_signals.compute_return_rates()
        return_rate = sku_data.get("return_rate_30d", config.RETURN_RATE_FALLBACK)

        # ── Raw velocity (gross order volume, before any buffering) ───────────
        count_14d = sum(1 for d in order_dates if d is not None and d >= cutoff_14d)
        count_7d = sum(1 for d in order_dates if d is not None and d >= cutoff_7d)

        avg_qty_per_line = (total_ordered / len(order_dates)) if order_dates else 0.0
        units_14d = count_14d * avg_qty_per_line
        units_7d = count_7d * avg_qty_per_line

        raw_velocity_14d = round(units_14d / config.VELOCITY_LONG_DAYS, 4)
        raw_velocity_7d = round(units_7d / config.VELOCITY_SHORT_DAYS, 4)

        # ── Net velocity = raw × (1 - cancel) × (1 - per-SKU return rate) ────
        net_multiplier = (1 - config.CANCEL_RATE) * (1 - return_rate)
        net_velocity_14d = round(raw_velocity_14d * net_multiplier, 4)
        net_velocity_7d = round(raw_velocity_7d * net_multiplier, 4)

        # ── Effective stock (accounts for committed pre-orders) ───────────────
        effective_stock = max(current_stock - preorder_qty, 0)

        if net_velocity_14d > 0:
            days_remaining = round(effective_stock / net_velocity_14d, 1)
        else:
            days_remaining = _VERY_HIGH_DAYS

        # ── Urgency tier ──────────────────────────────────────────────────────
        if days_remaining <= config.URGENCY_CRITICAL_THRESHOLD:
            urgency_tier = _URGENCY_CRITICAL
        elif days_remaining <= config.URGENCY_WARNING_THRESHOLD:
            urgency_tier = _URGENCY_WARNING
        else:
            urgency_tier = _URGENCY_HEALTHY

        enriched.append(
            {
                **sku_data,
                "raw_velocity_14d": raw_velocity_14d,
                "raw_velocity_7d": raw_velocity_7d,
                "net_velocity_14d": net_velocity_14d,
                "net_velocity_7d": net_velocity_7d,
                # Aliases so sheets/email/reorder code doesn't need changes
                "daily_velocity_14d": net_velocity_14d,
                "daily_velocity_7d": net_velocity_7d,
                "effective_stock": effective_stock,
                "days_remaining": days_remaining,
                "urgency_tier": urgency_tier,
            }
        )

    critical = sum(1 for s in enriched if s["urgency_tier"] == _URGENCY_CRITICAL)
    warning = sum(1 for s in enriched if s["urgency_tier"] == _URGENCY_WARNING)
    healthy = sum(1 for s in enriched if s["urgency_tier"] == _URGENCY_HEALTHY)

    logger.info(
        "Velocity computed for %d SKUs — 🔴 Critical: %d | 🟡 Warning: %d | 🟢 Healthy: %d "
        "(cancel buffer: %.0f%% | per-SKU return rates applied)",
        len(enriched), critical, warning, healthy,
        config.CANCEL_RATE * 100,
    )
    return enriched


def filter_actionable(skus: list[dict]) -> list[dict]:
    """Return only CRITICAL and WARNING SKUs, sorted by days_remaining ascending."""
    actionable = [s for s in skus if s["urgency_tier"] in (_URGENCY_CRITICAL, _URGENCY_WARNING)]
    return sorted(actionable, key=lambda s: s["days_remaining"])
