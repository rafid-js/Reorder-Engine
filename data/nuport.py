"""
Pull current stock levels from Nuport integration API.

Nuport integration endpoints all live under /integration/ and use a plain
API key header (no "Bearer" prefix). Pagination is page-based (0-indexed)
with a {count, page, pageSize, results} response envelope.

Shipment history is NOT available via the integration API (no bulk list
endpoint exists — only single-order lookup). WooCommerce is used for
order velocity instead.

pull_shipments() is kept as a stub returning empty data so the rest of the
pipeline doesn't break — Nuport shipment velocity was always zero because
the endpoint never worked. WooCommerce covers this.

pull_stock() hits GET /integration/inventory and extracts SKU + quantity
from each inventory item's nested product object.
"""

import time
from collections import defaultdict
from typing import Any

import requests

import config
from config import logger

# Plain API key — NO "Bearer" prefix (Nuport integration API requirement)
_HEADERS = {
    "Authorization": config.NUPORT_API_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _nuport_get(path: str, params: dict[str, Any] | None = None) -> list[dict]:
    """
    GET from Nuport integration API with retry + page-based pagination.

    Nuport response envelope: {count, page, pageSize, results}
    Page numbering starts at 0. Pass page=-1 to get all without pagination.
    """
    url = f"{config.NUPORT_BASE_URL}/{path.lstrip('/')}"
    params = params or {}
    page_size = params.pop("pageSize", 50)

    results: list[dict] = []
    page = 0

    while True:
        request_params = {**params, "page": page, "pageSize": page_size}
        last_exc: Exception | None = None

        for attempt in range(1, config.MAX_API_RETRIES + 1):
            try:
                resp = requests.get(url, params=request_params, headers=_HEADERS, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.RequestException as exc:
                last_exc = exc
                wait = config.RETRY_BACKOFF_BASE ** attempt
                logger.warning(
                    "Nuport GET %s page %d attempt %d failed: %s — retrying in %ds",
                    path, page, attempt, exc, wait,
                )
                time.sleep(wait)
        else:
            raise RuntimeError(
                f"Nuport API failed after {config.MAX_API_RETRIES} retries: {last_exc}"
            )

        if isinstance(data, list):
            batch = data
            total_count = len(batch)
        elif isinstance(data, dict):
            batch = data.get("results", [])
            total_count = data.get("count", len(batch))
        else:
            batch = []
            total_count = 0

        if not batch:
            break

        results.extend(batch)

        # Stop if we've got everything
        if len(results) >= total_count or len(batch) < page_size:
            break

        page += 1

    return results


def pull_shipments() -> tuple[list[dict], dict[str, int]]:
    """
    Stub — Nuport integration API has no bulk orders list endpoint.
    WooCommerce is used for order velocity. Returns empty data.
    """
    logger.info(
        "Nuport shipment pull skipped — no bulk list endpoint in integration API. "
        "WooCommerce covers order velocity."
    )
    return [], {}


def pull_stock() -> dict[str, int]:
    """
    Pull current stock on hand from GET /integration/inventory.

    Each result has a nested `product` object with `sku`, and a top-level
    `quantity` field (available stock — can be negative if oversold).
    processingQuantity = orders being packed/shipped (reduces effective stock).

    Returns dict: {SKU (uppercase) -> quantity_on_hand}
    """
    logger.info("Pulling Nuport stock levels from /integration/inventory...")

    try:
        inventory = _nuport_get("integration/inventory", {"pageSize": 50})
    except RuntimeError as exc:
        logger.error("Failed to pull Nuport stock levels: %s", exc)
        return {}

    stock: dict[str, int] = defaultdict(int)

    for item in inventory:
        product = item.get("product") or {}
        sku = (product.get("sku") or "").strip().upper()
        if not sku:
            continue

        # quantity = warehouse on-hand (can be negative when oversold)
        # processingQuantity = picked/being packed, still counts as available demand buffer
        qty = int(item.get("quantity", 0) or 0)
        stock[sku] += max(qty, 0)  # treat negative (oversold) as 0

    result = dict(stock)
    logger.info("Nuport stock data pulled. %d SKUs with stock data.", len(result))
    return result
