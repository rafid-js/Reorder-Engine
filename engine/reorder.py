"""
Reorder quantity formula and cost estimation.

Formula (moderate aggression):
  raw_qty = (velocity_14d × REORDER_HORIZON_DAYS × REORDER_BUFFER_MULTIPLIER) - current_stock
  reorder_qty = max(raw_qty, 0)
  If MOQ is known and reorder_qty < MOQ: round up to nearest MOQ multiple.

Adds to each SKU dict:
  reorder_qty:    int   — units to order
  estimated_cost: float — reorder_qty × last_purchase_price (BDT)
"""

import math

import config
from config import logger


def compute_reorder_quantities(skus: list[dict]) -> list[dict]:
    """
    Compute reorder quantity and estimated cost for every SKU.

    Works on all SKUs (not just triggered ones) so the caller can filter.
    """
    result: list[dict] = []

    for sku_data in skus:
        velocity = sku_data.get("daily_velocity_14d", 0.0)
        current_stock = sku_data.get("current_stock", 0)
        moq = sku_data.get("moq", 0)
        price = sku_data.get("last_purchase_price", 0.0)

        # Core formula
        target_supply = velocity * config.REORDER_HORIZON_DAYS * config.REORDER_BUFFER_MULTIPLIER
        raw_qty = target_supply - current_stock

        if raw_qty <= 0:
            reorder_qty = 0
        else:
            reorder_qty = math.ceil(raw_qty)

        # Round up to MOQ if applicable
        if moq and moq > 1 and reorder_qty > 0:
            reorder_qty = math.ceil(reorder_qty / moq) * moq

        estimated_cost = round(reorder_qty * price, 2) if price else 0.0

        result.append(
            {
                **sku_data,
                "reorder_qty": reorder_qty,
                "estimated_cost": estimated_cost,
            }
        )

    total_cost = sum(s["estimated_cost"] for s in result if s["reorder_qty"] > 0)
    triggered = sum(1 for s in result if s["reorder_qty"] > 0)
    logger.info(
        "Reorder quantities computed. %d SKUs need reordering. "
        "Total estimated cost: BDT %.2f",
        triggered, total_cost,
    )
    return result
