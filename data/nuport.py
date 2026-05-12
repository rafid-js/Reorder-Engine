"""
Pull shipment/delivery data and current stock levels from Nuport API.

Nuport is the OMS/logistics layer for Winterfell.

Statuses pulled: pending, on-hold, in-transit, delivered
Statuses ignored: flagged (returns), cancelled (no-answer / customer rejected)
  — flagged = return; cancelled = customer didn't receive call or cancelled.
    Both are excluded from demand calculation. The cancel/return rates are
    applied as a buffer multiplier in engine/velocity.py instead.
  — on-hold = PRE-ORDERS. Tracked separately as `preorder_qty` per SKU.
    This is critical for Winterfell to understand committed demand before
    the product is even in stock.

Returns three things:
  - shipments:    list of {sku, quantity, shipment_status, shipment_date}
                  (all active statuses except flagged/cancelled)
  - preorders:    dict of {sku -> preorder_qty}  (on-hold shipments only)
  - stock:        dict of {sku -> current_stock_on_hand}
"""

import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Any

import requests

import config
from config import logger

_HEADERS = {
    "Authorization": f"Bearer {config.NUPORT_API_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _nuport_get(path: str, params: dict[str, Any] | None = None) -> list[dict]:
    """GET from Nuport API with retry + pagination support."""
    url = f"{config.NUPORT_BASE_URL}/{path.lstrip('/')}"
    params = params or {}
    params.setdefault("limit", 100)

    results: list[dict] = []
    offset = 0

    while True:
        params["offset"] = offset
        last_exc: Exception | None = None

        for attempt in range(1, config.MAX_API_RETRIES + 1):
            try:
                resp = requests.get(url, params=params, headers=_HEADERS, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.RequestException as exc:
                last_exc = exc
                wait = config.RETRY_BACKOFF_BASE ** attempt
                logger.warning(
                    "Nuport GET %s offset %d attempt %d failed: %s — retrying in %ds",
                    path, offset, attempt, exc, wait,
                )
                time.sleep(wait)
        else:
            raise RuntimeError(
                f"Nuport API failed after {config.MAX_API_RETRIES} retries: {last_exc}"
            )

        # Nuport may return {"results": [...], "count": N} or a plain list
        if isinstance(data, list):
            batch = data
        elif isinstance(data, dict):
            batch = data.get("results", data.get("data", data.get("shipments", [])))
        else:
            batch = []

        if not batch:
            break

        results.extend(batch)

        if len(batch) < params["limit"]:
            break

        offset += params["limit"]

    return results


def _since_date() -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.LOOKBACK_DAYS)
    return cutoff.strftime("%Y-%m-%d")


def _extract_items(shipment: dict) -> list[dict]:
    """Extract line items from a shipment, trying common key names."""
    return (
        shipment.get("items")
        or shipment.get("line_items")
        or shipment.get("products")
        or []
    )


def _parse_date(shipment: dict) -> datetime | None:
    date_str = (
        shipment.get("delivered_at")
        or shipment.get("delivery_date")
        or shipment.get("updated_at")
        or shipment.get("created_at")
        or ""
    )
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def pull_shipments() -> tuple[list[dict], dict[str, int]]:
    """
    Pull all active shipments for the last LOOKBACK_DAYS.

    Pulls: pending, on-hold, in-transit, delivered
    Ignores: flagged (returns), cancelled

    Returns:
      - records:   flat list of {sku, quantity, shipment_status, shipment_date}
      - preorders: dict of {sku -> qty} for on-hold shipments only
    """
    logger.info("Pulling Nuport shipment data...")

    all_shipments: list[dict] = []

    for status in config.NUPORT_ACTIVE_STATUSES:
        try:
            batch = _nuport_get(
                "shipments",
                {"from_date": _since_date(), "status": status},
            )
            all_shipments.extend(batch)
            logger.info("  Nuport status='%s': %d shipments fetched.", status, len(batch))
        except RuntimeError as exc:
            logger.error("Failed to pull Nuport status='%s': %s", status, exc)

    records: list[dict] = []
    preorders: dict[str, int] = defaultdict(int)

    for shipment in all_shipments:
        status = shipment.get("status", "")
        shipment_date = _parse_date(shipment)
        is_preorder = (status.lower() == config.NUPORT_PREORDER_STATUS)

        for item in _extract_items(shipment):
            sku = (item.get("sku") or "").strip().upper()
            if not sku:
                continue

            qty = int(item.get("quantity", 0))

            if is_preorder:
                # Pre-orders tracked separately — committed demand, not yet delivered
                preorders[sku] += qty
            else:
                records.append(
                    {
                        "sku": sku,
                        "quantity": qty,
                        "shipment_status": status,
                        "shipment_date": shipment_date,
                    }
                )

    sku_count = len({r["sku"] for r in records})
    logger.info(
        "Nuport shipment data pulled. %d SKUs across %d records. "
        "%d SKUs have pre-order (on-hold) qty.",
        sku_count, len(records), len(preorders),
    )
    return records, dict(preorders)


def pull_stock() -> dict[str, int]:
    """
    Pull current stock on hand from Nuport inventory endpoint.

    Returns dict: {SKU (uppercase) -> quantity_on_hand}
    """
    logger.info("Pulling Nuport stock levels...")

    try:
        inventory = _nuport_get("inventory")
    except RuntimeError as exc:
        logger.error("Failed to pull Nuport stock levels: %s", exc)
        return {}

    stock: dict[str, int] = {}

    for item in inventory:
        sku = (item.get("sku") or "").strip().upper()
        if not sku:
            continue

        qty = int(
            item.get("quantity_on_hand")
            or item.get("stock")
            or item.get("available_quantity")
            or 0
        )
        stock[sku] = stock.get(sku, 0) + qty

    logger.info("Nuport stock data pulled. %d SKUs with stock data.", len(stock))
    return stock
