"""
Merge data from WooCommerce, Nuport, and Zoho Books into a unified per-SKU dict.

Master key: SKU (always normalized to uppercase)

Output schema per SKU:
{
    "sku":                  str,
    "product_name":         str,
    "total_ordered":        int,      # sum of all WC line items (pending+processing+on-hold+completed)
    "total_shipped":        int,      # Nuport shipments in active statuses (excl. flagged/cancelled)
    "total_delivered":      int,      # Nuport delivered-only shipments
    "true_demand":          int,      # total_ordered - total_delivered (raw unfulfilled demand)
    "preorder_qty":         int,      # Nuport on-hold qty (pre-orders placed, not yet dispatched)
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
    nuport_shipments: list[dict],
    nuport_preorders: dict[str, int],
    nuport_stock: dict[str, int],
    zoho_pos: dict[str, dict],
) -> list[dict]:
    """
    Merge all data sources into a unified per-SKU list.

    Any source may be empty/partial; the merger handles missing data gracefully
    and records which sources contributed via the `data_sources` field.

    nuport_shipments: all active (non-flagged, non-cancelled) shipment line items
    nuport_preorders: {sku -> qty} for on-hold (pre-order) shipments only
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

    # ── Aggregate Nuport shipments ────────────────────────────────────────────
    # All active statuses (pending + in-transit + delivered) — on-hold already separated
    nuport_shipped_agg: dict[str, int] = defaultdict(int)
    nuport_delivered_agg: dict[str, int] = defaultdict(int)

    for rec in nuport_shipments:
        sku = rec["sku"]  # already uppercased by nuport.py
        nuport_shipped_agg[sku] += rec["quantity"]
        if rec.get("shipment_status", "").lower() == "delivered":
            nuport_delivered_agg[sku] += rec["quantity"]

    # ── WooCommerce SKUs are the master list ──────────────────────────────────
    # Nuport inventory contains old/inactive SKUs not in the active catalogue.
    # Only process SKUs that have WooCommerce order history — this is the
    # source of truth for what Winterfell actually sells.
    all_skus: set[str] = set(wc_agg.keys())

    merged: list[dict] = []

    for sku in sorted(all_skus):
        wc = wc_agg.get(sku, {})
        total_shipped = nuport_shipped_agg.get(sku, 0)
        total_delivered = nuport_delivered_agg.get(sku, 0)
        preorder_qty = nuport_preorders.get(sku, 0)
        stock = nuport_stock.get(sku, 0)
        zoho = zoho_pos.get(sku, {})

        total_ordered = wc.get("total_ordered", 0)
        # True demand = orders placed but not yet delivered (raw, before cancel/return buffer)
        true_demand = max(total_ordered - total_delivered, 0)

        data_sources: list[str] = []
        if sku in wc_agg:
            data_sources.append("woocommerce")
        if sku in nuport_shipped_agg:
            data_sources.append("nuport_shipments")
        if sku in nuport_preorders:
            data_sources.append("nuport_preorders")
        if sku in nuport_stock:
            data_sources.append("nuport_stock")
        if sku in zoho_pos:
            data_sources.append("zoho_books")

        merged.append(
            {
                "sku": sku,
                "product_name": wc.get("product_name") or zoho.get("description", sku),
                "total_ordered": total_ordered,
                "total_shipped": total_shipped,
                "total_delivered": total_delivered,
                "true_demand": true_demand,
                "preorder_qty": preorder_qty,
                "current_stock": stock,
                "wc_order_dates": wc.get("order_dates", []),
                "supplier_name": zoho.get("supplier_name", ""),
                "last_purchase_price": float(zoho.get("last_purchase_price", 0)),
                "moq": int(zoho.get("moq", 0)),
                "data_sources": data_sources,
            }
        )

    preorder_sku_count = sum(1 for s in merged if s["preorder_qty"] > 0)
    logger.info(
        "Data merge complete. %d unique SKUs "
        "(WC: %d | Nuport shipped: %d | Nuport pre-orders: %d SKUs | Nuport stock: %d | Zoho: %d).",
        len(merged),
        len(wc_agg),
        len(nuport_shipped_agg),
        preorder_sku_count,
        len(nuport_stock),
        len(zoho_pos),
    )
    return merged
