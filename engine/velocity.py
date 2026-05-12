"""
Velocity calculations, cancel/return rate buffering, and urgency tiering.

Raw velocity from WooCommerce includes orders that will eventually cancel or be
returned, which would overstate consumption and cause over-ordering.

Buffer logic (based on Winterfell's historical rates):
  - ~27.5% of orders cancel before delivery (Nuport: flagged/cancelled)
  - ~15.0% of delivered orders are returned (Nuport: flagged = return)
  - Net fulfillment rate ≈ 72.6% of placed orders actually consume stock

We compute two velocity figures per SKU:
  - raw_velocity_14d / raw_velocity_7d:    straight order volume ÷ days
  - net_velocity_14d / net_velocity_7d:    raw × NET_FULFILLMENT_RATE

days_remaining and urgency tier use net_velocity so we don't trigger
false alarms from ghost demand.  The reorder formula also uses net_velocity
to avoid over-ordering.

Pre-orders (preorder_qty from Nuport on-hold) reduce effective stock
for days_remaining purposes: a pre-order is committed demand that will
draw down stock as soon as it dispatches.
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

    Input:  list of merged SKU dicts (from data/merger.py)
    Output: same list, each dict augmented with:
              raw_velocity_14d, raw_velocity_7d,
              net_velocity_14d, net_velocity_7d,
              effective_stock, days_remaining, urgency_tier
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

        # Count order line items within each velocity window
        count_14d = sum(1 for d in order_dates if d is not None and d >= cutoff_14d)
        count_7d = sum(1 for d in order_dates if d is not None and d >= cutoff_7d)

        # Scale line-item counts to units using average qty per line item
        avg_qty_per_line = (total_ordered / len(order_dates)) if order_dates else 0.0
        units_14d = count_14d * avg_qty_per_line
        units_7d = count_7d * avg_qty_per_line

        # Raw velocity (includes orders that will cancel/return — not used for decisions)
        raw_velocity_14d = round(units_14d / config.VELOCITY_LONG_DAYS, 4)
        raw_velocity_7d = round(units_7d / config.VELOCITY_SHORT_DAYS, 4)

        # Net velocity = only the portion of orders that actually consume stock
        # NET_FULFILLMENT_RATE = (1 - CANCEL_RATE) × (1 - RETURN_RATE) ≈ 0.726
        net_velocity_14d = round(raw_velocity_14d * config.NET_FULFILLMENT_RATE, 4)
        net_velocity_7d = round(raw_velocity_7d * config.NET_FULFILLMENT_RATE, 4)

        # Effective stock = warehouse stock minus pre-orders already committed
        # Pre-orders are real demand that will draw down stock when dispatched
        effective_stock = max(current_stock - preorder_qty, 0)

        if net_velocity_14d > 0:
            days_remaining = round(effective_stock / net_velocity_14d, 1)
        else:
            days_remaining = _VERY_HIGH_DAYS

        # Urgency tier based on net velocity and effective (post-preorder) stock
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
                # Keep daily_velocity_14d / 7d aliases pointing to net values
                # so downstream code (reorder.py, sheets.py) works unchanged
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
        "(cancel buffer: %.0f%% | return buffer: %.0f%% | net fulfillment: %.1f%%)",
        len(enriched), critical, warning, healthy,
        config.CANCEL_RATE * 100,
        config.RETURN_RATE * 100,
        config.NET_FULFILLMENT_RATE * 100,
    )
    return enriched


def filter_actionable(skus: list[dict]) -> list[dict]:
    """Return only CRITICAL and WARNING SKUs, sorted by days_remaining ascending."""
    actionable = [s for s in skus if s["urgency_tier"] in (_URGENCY_CRITICAL, _URGENCY_WARNING)]
    return sorted(actionable, key=lambda s: s["days_remaining"])
