"""
Merge data from WooCommerce, Nuport, and Zoho Books into a unified per-SKU dict.

Master key: SKU (always normalized to uppercase)

Output schema per SKU:
{
    "sku":                  str,
    "product_name":         str,
    "total_ordered":        int,      # sum of all WC line items
    "total_delivered":      int,      # sum of all Nuport delivered items
    "true_demand":          int,      # total_ordered - total_delivered
    "current_stock":        int,      # from Nuport inventory
    "wc_order_dates":       list[datetime],
    "supplier_name":        str,
    "last_purchase_price":  float,    # BDT
    "moq":                  int,
    "data_sources":         list[str],  # which sources contributed data
}
"""

from collections import defaultdict
from datetime import datetime
from typing import Any

from config import logger


def merge(
    wc_orders: list[dict],
    nuport_deliveries: list[dict],
    nuport_stock: dict[str, int],
    zoho_pos: dict[str, dict],
) -> list[dict]:
    """
    Merge all data sources into a unified per-SKU list.

    Any source may be empty/partial; the merger handles missing data gracefully
    and records which sources contributed via the `data_sources` field.
    """

    # ── Aggregate WooCommerce orders ──────────────────────────────────────────
    wc_agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"product_name": "", "total_ordered": 0, "order_dates": []}
    )

    for rec in wc_orders:
        sku = rec["sku"]  # already uppercased by woocommerce.py
        wc_agg[sku]["total_ordered"] += rec["quantity_ordered"]
        if rec.get("product_name"):
            wc_agg[sku]["product_name"] = rec["product_name"]
        if rec.get("order_date"):
            wc_agg[sku]["order_dates"].append(rec["order_date"])

    # ── Aggregate Nuport deliveries ───────────────────────────────────────────
    nuport_agg: dict[str, int] = defaultdict(int)

    for rec in nuport_deliveries:
        sku = rec["sku"]  # already uppercased by nuport.py
        nuport_agg[sku] += rec["quantity_delivered"]

    # ── Union of all SKUs seen across all sources ─────────────────────────────
    all_skus: set[str] = set(wc_agg.keys()) | set(nuport_agg.keys()) | set(nuport_stock.keys()) | set(zoho_pos.keys())

    merged: list[dict] = []

    for sku in sorted(all_skus):
        wc = wc_agg.get(sku, {})
        delivered = nuport_agg.get(sku, 0)
        stock = nuport_stock.get(sku, 0)
        zoho = zoho_pos.get(sku, {})

        total_ordered = wc.get("total_ordered", 0)
        true_demand = max(total_ordered - delivered, 0)

        # Track which sources contributed data for transparency in reports
        data_sources: list[str] = []
        if sku in wc_agg:
            data_sources.append("woocommerce")
        if sku in nuport_agg:
            data_sources.append("nuport_deliveries")
        if sku in nuport_stock:
            data_sources.append("nuport_stock")
        if sku in zoho_pos:
            data_sources.append("zoho_books")

        merged.append(
            {
                "sku": sku,
                "product_name": wc.get("product_name") or zoho.get("description", sku),
                "total_ordered": total_ordered,
                "total_delivered": delivered,
                "true_demand": true_demand,
                "current_stock": stock,
                "wc_order_dates": wc.get("order_dates", []),
                "supplier_name": zoho.get("supplier_name", ""),
                "last_purchase_price": float(zoho.get("last_purchase_price", 0)),
                "moq": int(zoho.get("moq", 0)),
                "data_sources": data_sources,
            }
        )

    logger.info(
        "Data merge complete. %d unique SKUs across all sources "
        "(WC: %d, Nuport deliveries: %d, Nuport stock: %d, Zoho: %d).",
        len(merged),
        len(wc_agg),
        len(nuport_agg),
        len(nuport_stock),
        len(zoho_pos),
    )
    return merged
