"""
Pull shipment/delivery data and current stock levels from Nuport API.

Nuport is the OMS/logistics layer for Winterfell.

Returns two things:
  - deliveries: list of {sku, quantity_delivered, delivery_status, delivery_date}
  - stock:      dict of {sku -> current_stock_on_hand}
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

import config
from config import logger

_HEADERS = {
    "Authorization": f"Bearer {config.NUPORT_API_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _nuport_get(path: str, params: dict[str, Any] | None = None) -> Any:
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

        # If fewer records than limit were returned, we've reached the end
        if len(batch) < params["limit"]:
            break

        offset += params["limit"]

    return results


def _since_date() -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.LOOKBACK_DAYS)
    return cutoff.strftime("%Y-%m-%d")


def pull_deliveries() -> list[dict]:
    """
    Pull all shipment/delivery records for the last LOOKBACK_DAYS.

    Returns flat list of line-item-level dicts keyed by SKU.
    """
    logger.info("Pulling Nuport delivery data...")

    try:
        shipments = _nuport_get(
            "shipments",
            {"from_date": _since_date(), "status": "delivered"},
        )
    except RuntimeError as exc:
        logger.error("Failed to pull Nuport deliveries: %s", exc)
        return []

    records: list[dict] = []

    for shipment in shipments:
        delivery_date_str = (
            shipment.get("delivered_at")
            or shipment.get("delivery_date")
            or shipment.get("updated_at")
            or ""
        )
        try:
            delivery_date = datetime.fromisoformat(delivery_date_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            delivery_date = None

        delivery_status = shipment.get("status", "delivered")

        # Line items may be under "items", "line_items", or "products"
        items = (
            shipment.get("items")
            or shipment.get("line_items")
            or shipment.get("products")
            or []
        )

        for item in items:
            sku = (item.get("sku") or "").strip().upper()
            if not sku:
                continue

            records.append(
                {
                    "sku": sku,
                    "quantity_delivered": int(item.get("quantity", 0)),
                    "delivery_status": delivery_status,
                    "delivery_date": delivery_date,
                }
            )

    sku_count = len({r["sku"] for r in records})
    logger.info(
        "Nuport delivery data pulled. %d SKUs found across %d delivery records.",
        sku_count,
        len(records),
    )
    return records


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
