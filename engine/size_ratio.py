"""
Size Ratio / Cutting Ratio Engine.

Uses SS (Stock-adjusted) logic to compute how many units of each size
to cut in the next production run:

  gap          = 30d_orders - current_stock
  cutting_ratio = gap / reference_gap, rounded to nearest 0.5
  reference_gap = gap of the smallest size (natural order) with a positive gap

Ratios are always 0, 0.5, 1, 1.5, 2, 2.5, 3 … — never decimals in between.
Overstocked sizes (gap ≤ 0) get ratio 0 and are skipped in production.

Output: for a total production run of N pieces, each size gets:
    qty = round(cutting_ratio / sum_of_ratios * N)

Two SKU variant patterns:
  Pattern A — text size suffix:   "TS-042-XL"   → parent "TS-042",  size "XL"
  Pattern B — numeric variant ID: "34404-53666"  → parent "34404",  size from
              WC attributes (priority) or product name trailing token "... - 30"
"""

import json
import re
from collections import defaultdict
from typing import Any

import config
from config import logger

_HISTORY_PATH = config.BASE_DIR / "logs" / "size_history.json"

_VARIANT_ID_RE = re.compile(r"^\d{4,}$")
_NAME_SIZE_RE  = re.compile(r"\s*-\s*(\S+)\s*$")


# ── History ────────────────────────────────────────────────────────────────────

def _load_size_history() -> dict[str, dict[str, int]]:
    if not _HISTORY_PATH.exists():
        return {}
    try:
        with open(_HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load size history: %s", exc)
        return {}


def _save_size_history(data: dict[str, dict[str, int]]) -> None:
    try:
        _HISTORY_PATH.parent.mkdir(exist_ok=True)
        with open(_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        logger.warning("Could not save size history: %s", exc)


# ── SKU parsing ────────────────────────────────────────────────────────────────

def _extract_size_from_name(product_name: str) -> str | None:
    if not product_name:
        return None
    m = _NAME_SIZE_RE.search(product_name.strip())
    return m.group(1).upper() if m else None


def parse_size_variant(
    sku: str,
    product_name: str = "",
    size_map: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """
    Extract (parent_sku, size) from a size-variant SKU.
    Pattern A: last dash-segment in KNOWN_SIZES → ("TS-042", "XL")
    Pattern B: last dash-segment is 4+ digit number →
               size from WC attributes map, then product name suffix.
    """
    parts = sku.split("-")
    if len(parts) < 2:
        return None

    last   = parts[-1].upper()
    parent = "-".join(parts[:-1])

    if last in config.KNOWN_SIZES:
        return parent, last

    if _VARIANT_ID_RE.match(parts[-1]):
        if size_map and sku in size_map and size_map[sku]:
            return parent, size_map[sku]
        size = _extract_size_from_name(product_name)
        if size:
            return parent, size

    return None


def _size_sort_key(size: str) -> tuple:
    """Numeric sizes sort ascending by value; text sizes by SIZE_DISPLAY_ORDER."""
    if size.isdigit():
        return (0, int(size), "")
    order_map = {s: i for i, s in enumerate(config.SIZE_DISPLAY_ORDER)}
    return (1, order_map.get(size, 999), size)


# ── Cutting ratio (SS logic) ───────────────────────────────────────────────────

def _round_to_half(x: float) -> float:
    """Round to nearest 0.5 step: 0, 0.5, 1, 1.5, 2, 2.5, 3 …"""
    return round(x * 2) / 2


def _compute_cutting_ratios(size_data: list[dict]) -> None:
    """
    Add gap and cutting_ratio to each size dict in-place.

    gap          = 30d_orders - current_stock
    reference    = gap of smallest size (natural order) with gap > 0
    cutting_ratio = round_to_half(gap / reference), 0 when gap <= 0
    """
    for d in size_data:
        d["gap"] = d["total_ordered_30d"] - d["current_stock"]

    # Sort by size to find the reference (smallest with positive gap)
    positive = sorted(
        [d for d in size_data if d["gap"] > 0],
        key=lambda x: _size_sort_key(x["size"]),
    )

    if not positive:
        for d in size_data:
            d["cutting_ratio"] = 0.0
        return

    reference_gap = positive[0]["gap"]

    for d in size_data:
        if d["gap"] <= 0:
            d["cutting_ratio"] = 0.0
        else:
            d["cutting_ratio"] = _round_to_half(d["gap"] / reference_gap)


# ── Core engine ────────────────────────────────────────────────────────────────

def compute_size_ratios(
    enriched_skus: list[dict],
    product_categories: dict[str, str],
    sku_size_map: dict[str, str] | None = None,
) -> list[dict]:
    """
    Group size-variant sub-SKUs by parent and compute cutting ratios.

    Returns list of parent-SKU dicts sorted by total 30d sales descending
    (best-selling products first). Each dict contains:

      parent_sku, product_name, category,
      total_ordered_30d,   # total 30d orders across all sizes of this product
      total_reorder_qty,   # sum of reorder quantities from reorder engine
      sizes: [
        {
          size, sku, total_ordered_30d, orders_pct,
          current_stock, gap, cutting_ratio,
          suggested_qty,    # cutting_ratio / sum_of_ratios * total_reorder_qty
          per_100_cuts,     # cutting_ratio / sum_of_ratios * 100 (rounded)
          change_vs_last,   # delta vs previous run's suggested_qty
          health_flag,
        }, ...
      ]
    """
    sku_map: dict[str, dict] = {s["sku"]: s for s in enriched_skus}

    groups: dict[str, list[str]] = defaultdict(list)
    for sku, data in sku_map.items():
        parsed = parse_size_variant(sku, data.get("product_name", ""), sku_size_map)
        if parsed:
            parent, _ = parsed
            groups[parent].append(sku)

    groups = {p: skus for p, skus in groups.items() if len(skus) >= 2}

    if not groups:
        logger.info("Size ratio engine: no multi-size parent SKUs found.")
        return []

    history = _load_size_history()
    new_history: dict[str, dict[str, int]] = {}
    results: list[dict] = []

    for parent_sku, sub_skus in groups.items():
        category = product_categories.get(parent_sku, "Uncategorized")

        # Build per-size data
        size_data: list[dict[str, Any]] = []
        for sub_sku in sub_skus:
            sub_data = sku_map[sub_sku]
            parsed = parse_size_variant(sub_sku, sub_data.get("product_name", ""), sku_size_map)
            if not parsed:
                continue
            _, size = parsed
            s = sub_data
            size_data.append({
                "size":             size,
                "sku":              sub_sku,
                "current_stock":    s.get("current_stock", 0),
                "total_ordered_30d": s.get("total_ordered", 0),
                "reorder_qty":      s.get("reorder_qty", 0),
            })

        if not size_data:
            continue

        # Sort sizes in natural order before computing ratios
        size_data.sort(key=lambda x: _size_sort_key(x["size"]))

        # SS cutting ratio logic
        _compute_cutting_ratios(size_data)

        total_ordered_30d = sum(d["total_ordered_30d"] for d in size_data)
        total_reorder     = sum(d["reorder_qty"] for d in size_data)
        total_ratio       = sum(d["cutting_ratio"] for d in size_data)

        # Build final size results
        size_results: list[dict[str, Any]] = []
        parent_history_new: dict[str, int] = {}

        for d in size_data:
            ratio  = d["cutting_ratio"]
            orders = d["total_ordered_30d"]
            stock  = d["current_stock"]
            gap    = d["gap"]

            orders_pct = round(orders / total_ordered_30d * 100, 1) if total_ordered_30d > 0 else 0.0

            if total_ratio > 0:
                suggested_qty = round(ratio / total_ratio * total_reorder)
                per_100_cuts  = round(ratio / total_ratio * 100)
            else:
                suggested_qty = 0
                per_100_cuts  = 0

            # Health flag
            if stock == 0 and orders > 0:
                health_flag = "💀 STOCKOUT"
            elif gap > orders * 0.5:
                health_flag = "⚠️ UNDERSTOCKED"
            elif gap < 0:
                health_flag = "📦 OVERSTOCKED"
            else:
                health_flag = "OK"

            prev_qty = history.get(parent_sku, {}).get(d["size"])
            change   = (suggested_qty - prev_qty) if prev_qty is not None else None
            parent_history_new[d["size"]] = suggested_qty

            size_results.append({
                "size":             d["size"],
                "sku":              d["sku"],
                "total_ordered_30d": orders,
                "orders_pct":       orders_pct,
                "current_stock":    stock,
                "gap":              gap,
                "cutting_ratio":    ratio,
                "suggested_qty":    suggested_qty,
                "per_100_cuts":     per_100_cuts,
                "change_vs_last":   change,
                "health_flag":      health_flag,
            })

        new_history[parent_sku] = parent_history_new

        # Product name: take first sub-SKU, strip trailing " - Size" suffix
        first_sub    = sku_map.get(sub_skus[0], {})
        product_name = _NAME_SIZE_RE.sub("", first_sub.get("product_name", parent_sku)).strip()

        results.append({
            "parent_sku":        parent_sku,
            "product_name":      product_name,
            "category":          category,
            "total_ordered_30d": total_ordered_30d,
            "total_reorder_qty": total_reorder,
            "sizes":             size_results,
        })

    _save_size_history(new_history)

    # Sort: best-selling products first
    results.sort(key=lambda r: -r["total_ordered_30d"])

    logger.info(
        "Size ratio engine: %d parent SKUs, sorted best-selling first.",
        len(results),
    )
    return results
