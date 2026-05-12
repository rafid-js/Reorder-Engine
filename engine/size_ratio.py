"""
Size Ratio Optimization Engine.

Supports two SKU variant patterns:

  Pattern A — text size suffix (legacy):
    "TS-042-XL"  →  parent="TS-042",  size="XL"
    Last dash-segment is a recognized size token (XS/S/M/L/XL/XXL…).

  Pattern B — Nuport/WooCommerce numeric variant ID:
    "34404-53666"  →  parent="34404",  size extracted from product name
    Last dash-segment is a 4+ digit numeric string (WooCommerce variation post
    ID). Size is the trailing token after " - " in the product name:
      "Chocolate Corduroy Loose Fit Pant - 30"  →  size="30"

Size history (previous_qty, change_vs_last) is persisted to logs/size_history.json
so each run can show delta vs the prior recommendation.
"""

import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import config
from config import logger

_HISTORY_PATH = config.BASE_DIR / "logs" / "size_history.json"

# Matches WooCommerce variation IDs: 4+ consecutive digits at end of SKU segment
_VARIANT_ID_RE = re.compile(r"^\d{4,}$")

# Matches trailing " - <token>" in product names
_NAME_SIZE_RE = re.compile(r"\s*-\s*(\S+)\s*$")


# ── History persistence ────────────────────────────────────────────────────────

def _load_size_history() -> dict[str, dict[str, int]]:
    """Load {parent_sku -> {size -> suggested_qty}} from history file."""
    if not _HISTORY_PATH.exists():
        return {}
    try:
        with open(_HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load size history: %s", exc)
        return {}


def _save_size_history(data: dict[str, dict[str, int]]) -> None:
    """Persist {parent_sku -> {size -> suggested_qty}} to history file."""
    try:
        _HISTORY_PATH.parent.mkdir(exist_ok=True)
        with open(_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        logger.warning("Could not save size history: %s", exc)


# ── SKU parsing ────────────────────────────────────────────────────────────────

def _extract_size_from_name(product_name: str) -> str | None:
    """
    Pull the size token from a product name like 'Chocolate Corduroy Loose Fit Pant - 30'.
    Returns uppercase string ('30', 'XL', etc.) or None if no match.
    """
    if not product_name:
        return None
    m = _NAME_SIZE_RE.search(product_name.strip())
    return m.group(1).upper() if m else None


def parse_size_variant(sku: str, product_name: str = "") -> tuple[str, str] | None:
    """
    Extract (parent_sku, size) from a size-variant SKU.

    Pattern A — text size token in SKU:
        "TS-042-XL"   -> ("TS-042", "XL")

    Pattern B — numeric WooCommerce variation ID in SKU, size in product name:
        "34404-53666" with name "... - 30" -> ("34404", "30")

    Returns None if neither pattern matches.
    """
    parts = sku.split("-")
    if len(parts) < 2:
        return None

    last = parts[-1].upper()

    # Pattern A: last segment is a known size token
    if last in config.KNOWN_SIZES:
        return "-".join(parts[:-1]), last

    # Pattern B: last segment is a 4+ digit numeric variant ID
    if _VARIANT_ID_RE.match(parts[-1]):
        size = _extract_size_from_name(product_name)
        if size:
            return "-".join(parts[:-1]), size

    return None


def _size_sort_key(size: str) -> tuple:
    """Sort key: numeric sizes (30, 32…) by value; text sizes by SIZE_DISPLAY_ORDER."""
    if size.isdigit():
        return (0, int(size), "")
    order_map = {s: i for i, s in enumerate(config.SIZE_DISPLAY_ORDER)}
    return (1, order_map.get(size, 999), size)


# ── Core engine ────────────────────────────────────────────────────────────────

def compute_size_ratios(
    enriched_skus: list[dict],
    product_categories: dict[str, str],
) -> list[dict]:
    """
    Group size-variant sub-SKUs by parent SKU and compute per-size analysis.

    Returns a list of parent-SKU dicts, each containing:
      parent_sku, product_name, category, total_reorder_qty,
      sizes: [
        {
          size, sku, current_stock, net_velocity_14d,
          size_units_14d,       # units sold in SIZE_VELOCITY_WINDOW days
          size_ratio_pct,       # % of parent's total velocity this size represents
          suggested_qty,        # normalized share of total_reorder_qty
          sell_through_pct,     # units_sold / (current_stock + units_sold)
          days_remaining,
          health_flag,          # 🔥 FAST_MOVER | 🧊 SLOW_MOVER | 💀 SIZE_STOCKOUT | ⚠️ OVERSTOCK_RISK | OK
          previous_qty,
          change_vs_last,
        },
        ...
      ]

    Only parent SKUs that have at least 2 recognized size variants are included.
    """
    # Build lookup: sku -> enriched dict
    sku_map: dict[str, dict] = {s["sku"]: s for s in enriched_skus}

    # Group sub-SKUs by parent — pass product_name so numeric variant IDs resolve
    groups: dict[str, list[str]] = defaultdict(list)
    for sku, data in sku_map.items():
        parsed = parse_size_variant(sku, data.get("product_name", ""))
        if parsed:
            parent, _ = parsed
            groups[parent].append(sku)

    # Only keep parents with 2+ size variants
    groups = {p: skus for p, skus in groups.items() if len(skus) >= 2}

    if not groups:
        logger.info("Size ratio engine: no multi-size parent SKUs found.")
        return []

    history = _load_size_history()
    new_history: dict[str, dict[str, int]] = {}
    results: list[dict] = []

    for parent_sku, sub_skus in sorted(groups.items()):
        # Resolve category — check parent SKU first, then sub-SKU prefix matches
        category = product_categories.get(parent_sku, "Uncategorized")

        # Compute per-size velocity data
        size_data: list[dict[str, Any]] = []
        total_velocity = 0.0

        for sub_sku in sub_skus:
            sub_data = sku_map[sub_sku]
            parsed = parse_size_variant(sub_sku, sub_data.get("product_name", ""))
            if not parsed:
                continue
            _, size = parsed
            s = sku_map[sub_sku]

            net_vel = s.get("net_velocity_14d") or s.get("daily_velocity_14d", 0.0)
            # Units sold over the size velocity window
            units_14d = net_vel * config.SIZE_VELOCITY_WINDOW
            current_stock = s.get("current_stock", 0)
            days_rem = s.get("days_remaining", 9999)

            size_data.append({
                "size": size,
                "sku": sub_sku,
                "current_stock": current_stock,
                "net_velocity_14d": net_vel,
                "size_units_14d": round(units_14d, 2),
                "days_remaining": days_rem,
                "reorder_qty": s.get("reorder_qty", 0),
            })
            total_velocity += net_vel

        if not size_data:
            continue

        # Total reorder qty across all sizes (used to normalize suggestions)
        total_reorder = sum(d["reorder_qty"] for d in size_data)

        # Ratio scores and suggested quantities
        size_results: list[dict[str, Any]] = []
        allocated = 0
        highest_vel_idx = max(range(len(size_data)), key=lambda i: size_data[i]["net_velocity_14d"])

        for i, d in enumerate(size_data):
            size = d["size"]
            net_vel = d["net_velocity_14d"]
            units_14d = d["size_units_14d"]
            current_stock = d["current_stock"]
            days_rem = d["days_remaining"]

            # Ratio as % of parent's total velocity
            ratio_pct = (net_vel / total_velocity * 100) if total_velocity > 0 else 0.0

            # Suggested qty = proportional share of total_reorder
            if total_velocity > 0 and i != highest_vel_idx:
                raw_suggested = (net_vel / total_velocity) * total_reorder
                suggested = math.ceil(raw_suggested)
                allocated += suggested
            else:
                suggested = None  # fill remainder to highest velocity size later

            # Sell-through % (units_sold / (stock + units_sold))
            denom = current_stock + units_14d
            sell_through = (units_14d / denom) if denom > 0 else 0.0

            # Health flag
            if days_rem <= 0 or current_stock == 0:
                health_flag = "💀 SIZE_STOCKOUT"
            elif sell_through >= config.SIZE_FAST_MOVER_SELL_THROUGH:
                health_flag = "🔥 FAST_MOVER"
            elif sell_through <= config.SIZE_SLOW_MOVER_SELL_THROUGH:
                health_flag = "🧊 SLOW_MOVER"
            elif net_vel > 0 and (current_stock / net_vel) > config.SIZE_OVERSTOCK_DAYS:
                health_flag = "⚠️ OVERSTOCK_RISK"
            else:
                health_flag = "OK"

            # History
            prev_qty = history.get(parent_sku, {}).get(size)
            change_vs_last = None  # set after suggested is finalized

            size_results.append({
                "size": size,
                "sku": d["sku"],
                "current_stock": current_stock,
                "net_velocity_14d": round(net_vel, 4),
                "size_units_14d": units_14d,
                "size_ratio_pct": round(ratio_pct, 1),
                "suggested_qty": suggested,
                "sell_through_pct": round(sell_through * 100, 1),
                "days_remaining": days_rem,
                "health_flag": health_flag,
                "previous_qty": prev_qty,
                "_highest_vel": (i == highest_vel_idx),
            })

        # Assign remainder to highest velocity size
        for sr in size_results:
            if sr["_highest_vel"]:
                sr["suggested_qty"] = max(total_reorder - allocated, 0)
            del sr["_highest_vel"]

        # Compute change_vs_last now that suggested_qty is final
        parent_history_new: dict[str, int] = {}
        for sr in size_results:
            size = sr["size"]
            suggested = sr["suggested_qty"]
            prev = sr["previous_qty"]
            sr["change_vs_last"] = (suggested - prev) if prev is not None else None
            parent_history_new[size] = suggested

        new_history[parent_sku] = parent_history_new

        # Sort sizes: numeric (waist/length) ascending, then text sizes by display order
        size_results.sort(key=lambda x: _size_sort_key(x["size"]))

        # Representative product name from the first sub-SKU with size suffix stripped
        first_sub = sku_map.get(sub_skus[0], {})
        product_name = first_sub.get("product_name", parent_sku)
        # Strip trailing " - <size>" regardless of whether size is text or numeric
        product_name = _NAME_SIZE_RE.sub("", product_name).strip()

        results.append({
            "parent_sku": parent_sku,
            "product_name": product_name,
            "category": category,
            "total_reorder_qty": total_reorder,
            "total_velocity": round(total_velocity, 4),
            "sizes": size_results,
        })

    _save_size_history(new_history)

    stockout_count = sum(
        1 for r in results for s in r["sizes"] if s["health_flag"] == "💀 SIZE_STOCKOUT"
    )
    logger.info(
        "Size ratio engine: %d parent SKUs analyzed, %d size stockouts detected.",
        len(results), stockout_count,
    )

    return results
