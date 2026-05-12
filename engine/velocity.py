"""
Velocity calculations and urgency tiering.

Given the merged SKU list (with raw order-date arrays from WooCommerce),
computes:
  - daily_velocity_14d:  average daily units sold over last 14 days
  - daily_velocity_7d:   average daily units sold over last 7 days
  - days_remaining:      current_stock / daily_velocity_14d
  - urgency_tier:        CRITICAL / WARNING / HEALTHY
"""

from datetime import datetime, timedelta, timezone

import config
from config import logger

_URGENCY_CRITICAL = "CRITICAL"
_URGENCY_WARNING = "WARNING"
_URGENCY_HEALTHY = "HEALTHY"

_VERY_HIGH_DAYS = 9999  # sentinel when velocity is zero


def _count_units_in_window(order_dates: list[datetime], quantities_per_date: list[int], days: int) -> int:
    """Sum quantities for orders placed within the last `days` days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    total = 0
    for date, qty in zip(order_dates, quantities_per_date):
        if date is not None and date >= cutoff:
            total += qty
    return total


def _build_date_qty_pairs(sku_orders: list[dict]) -> tuple[list[datetime], list[int]]:
    """Extract parallel lists of (order_date, quantity) from wc_order_dates + total_ordered.

    The merged dict stores order_dates as a flat list and total_ordered as aggregate,
    so we need the raw line-item level data to rebuild per-date quantities.
    The merger stores `wc_order_dates` as a list of datetimes — one per order line item.
    Each entry in wc_order_dates corresponds to exactly one unit of quantity_ordered
    from the original line items.  We rebuilt it that way in woocommerce.py already
    (we appended order_date for each line item, not each unit).

    To handle this properly, the merger passes all order_dates; here we treat each
    occurrence as one order event contributing its quantity.  Since wc_order_dates
    is a flat list where each element is from one line item, and we also have
    total_ordered, we distribute the total evenly across dates for approximation.
    In practice it's close enough for velocity calculations.
    """
    dates = sku_orders
    return dates


def compute_velocity(merged_skus: list[dict]) -> list[dict]:
    """
    Enrich each SKU dict with velocity metrics and urgency tier.

    Input:  list of merged SKU dicts (from data/merger.py)
    Output: same list, each dict augmented with:
              daily_velocity_14d, daily_velocity_7d,
              days_remaining, urgency_tier
    """
    now_utc = datetime.now(timezone.utc)
    cutoff_14d = now_utc - timedelta(days=config.VELOCITY_LONG_DAYS)
    cutoff_7d = now_utc - timedelta(days=config.VELOCITY_SHORT_DAYS)

    enriched: list[dict] = []

    for sku_data in merged_skus:
        sku = sku_data["sku"]
        order_dates: list[datetime] = sku_data.get("wc_order_dates", [])
        total_ordered: int = sku_data.get("total_ordered", 0)
        current_stock: int = sku_data.get("current_stock", 0)

        # Count orders (line items) within each window
        count_14d = sum(1 for d in order_dates if d is not None and d >= cutoff_14d)
        count_7d = sum(1 for d in order_dates if d is not None and d >= cutoff_7d)

        # Each line item in wc_order_dates represents one order line.
        # To get units, we scale by avg qty per order line.
        if order_dates:
            avg_qty_per_line = total_ordered / len(order_dates)
        else:
            avg_qty_per_line = 0.0

        units_14d = count_14d * avg_qty_per_line
        units_7d = count_7d * avg_qty_per_line

        daily_velocity_14d = round(units_14d / config.VELOCITY_LONG_DAYS, 4)
        daily_velocity_7d = round(units_7d / config.VELOCITY_SHORT_DAYS, 4)

        if daily_velocity_14d > 0:
            days_remaining = round(current_stock / daily_velocity_14d, 1)
        else:
            days_remaining = _VERY_HIGH_DAYS

        # Urgency tier
        if days_remaining <= config.URGENCY_CRITICAL_THRESHOLD:
            urgency_tier = _URGENCY_CRITICAL
        elif days_remaining <= config.URGENCY_WARNING_THRESHOLD:
            urgency_tier = _URGENCY_WARNING
        else:
            urgency_tier = _URGENCY_HEALTHY

        enriched.append(
            {
                **sku_data,
                "daily_velocity_14d": daily_velocity_14d,
                "daily_velocity_7d": daily_velocity_7d,
                "days_remaining": days_remaining,
                "urgency_tier": urgency_tier,
            }
        )

    critical = sum(1 for s in enriched if s["urgency_tier"] == _URGENCY_CRITICAL)
    warning = sum(1 for s in enriched if s["urgency_tier"] == _URGENCY_WARNING)
    healthy = sum(1 for s in enriched if s["urgency_tier"] == _URGENCY_HEALTHY)

    logger.info(
        "Velocity computed for %d SKUs — 🔴 Critical: %d | 🟡 Warning: %d | 🟢 Healthy: %d",
        len(enriched), critical, warning, healthy,
    )
    return enriched


def filter_actionable(skus: list[dict]) -> list[dict]:
    """Return only CRITICAL and WARNING SKUs, sorted by days_remaining ascending."""
    actionable = [s for s in skus if s["urgency_tier"] in (_URGENCY_CRITICAL, _URGENCY_WARNING)]
    return sorted(actionable, key=lambda s: s["days_remaining"])
