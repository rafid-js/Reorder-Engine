"""
Pull orders from WooCommerce REST API for the last LOOKBACK_DAYS days.

Statuses pulled: pending, processing, on-hold, completed
Statuses ignored: cancelled, refunded
  — cancelled/refunded are excluded because Nuport inaccurately reflects WC status
    and those orders represent true demand that was placed (even if unfulfilled).
  — on-hold is critical: Winterfell uses it for pre-orders.

Returns a list of dicts:
  {sku, product_name, quantity_ordered, order_date, order_status}
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

import config
from config import logger


def _wc_get(endpoint: str, params: dict[str, Any] | None = None) -> list[dict]:
    """GET from WooCommerce API with retry + pagination."""
    url = f"{config.WC_URL}/wp-json/wc/v3/{endpoint}"
    auth = (config.WC_CONSUMER_KEY, config.WC_CONSUMER_SECRET)
    params = params or {}
    params.setdefault("per_page", 100)

    results: list[dict] = []
    page = 1

    while True:
        params["page"] = page
        last_exc: Exception | None = None

        for attempt in range(1, config.MAX_API_RETRIES + 1):
            try:
                resp = requests.get(url, params=params, auth=auth, timeout=30)
                resp.raise_for_status()
                batch = resp.json()
                break
            except requests.RequestException as exc:
                last_exc = exc
                wait = config.RETRY_BACKOFF_BASE ** attempt
                logger.warning(
                    "WooCommerce GET %s page %d attempt %d failed: %s — retrying in %ds",
                    endpoint, page, attempt, exc, wait,
                )
                time.sleep(wait)
        else:
            raise RuntimeError(
                f"WooCommerce API failed after {config.MAX_API_RETRIES} retries: {last_exc}"
            )

        if not batch:
            break
        results.extend(batch)

        total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            break
        page += 1

    return results


def _since_date() -> str:
    """ISO-8601 cutoff date string for the API filter."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.LOOKBACK_DAYS)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S")


def pull_orders() -> list[dict]:
    """
    Pull all active orders from the last LOOKBACK_DAYS.

    Pulls: pending, processing, on-hold, completed
    Ignores: cancelled, refunded
      — Nuport inaccurately changes WC statuses, so we pull broadly
        and rely on the cancel/return rate buffer in the engine layer.
      — on-hold captures pre-orders placed by customers.

    Returns flat list of line-item-level dicts keyed by SKU.
    """
    logger.info("Pulling WooCommerce data...")

    raw_orders: list[dict] = []

    for status in config.WC_ACTIVE_STATUSES:
        orders = _wc_get(
            "orders",
            {
                "status": status,
                "after": _since_date(),
                "orderby": "date",
                "order": "desc",
            },
        )
        raw_orders.extend(orders)
        logger.info("  WooCommerce status='%s': %d orders fetched.", status, len(orders))

    records: list[dict] = []

    for order in raw_orders:
        order_date_str = order.get("date_created", "")
        order_status = order.get("status", "")

        try:
            order_date = datetime.fromisoformat(order_date_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            order_date = None

        for item in order.get("line_items", []):
            sku = (item.get("sku") or "").strip().upper()
            if not sku:
                # Fall back to product ID as pseudo-SKU so the item isn't lost
                product_id = item.get("product_id")
                if not product_id:
                    continue
                sku = f"WC-PRODUCT-{product_id}"

            records.append(
                {
                    "sku": sku,
                    "product_name": item.get("name", ""),
                    "quantity_ordered": int(item.get("quantity", 0)),
                    "order_date": order_date,
                    "order_status": order_status,
                }
            )

    sku_count = len({r["sku"] for r in records})
    logger.info("WooCommerce data pulled. %d SKUs found across %d line items.", sku_count, len(records))
    return records
