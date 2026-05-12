"""
Pull return data for per-SKU return rate calculations.

Primary source:   Nuport flagged tab (returns + COD refused)
Fallback source:  WooCommerce refunded orders

Fallback is used automatically when Nuport flagged returns no data (API down
or empty tab). WooCommerce refunded data is clean and SKU-accurate but runs
10-15% lower than Nuport flagged because WhatsApp/Messenger orders are not
captured in WooCommerce — this is acceptable for rate calculations.

Public API:
    pull_returns() -> (data, source_label)
        Tries Nuport first; falls back to WC if Nuport returns nothing.
        Returns the same dict shape regardless of source.

Internal:
    pull_flagged()      — Nuport flagged shipments
    pull_wc_refunded()  — WooCommerce refunded orders
"""

import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

import config
from config import logger

# ── Nuport helpers ────────────────────────────────────────────────────────────

_NUPORT_HEADERS = {
    "Authorization": f"Bearer {config.NUPORT_API_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _nuport_get(path: str, params: dict[str, Any] | None = None) -> list[dict]:
    """GET from Nuport API with retry + pagination."""
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
                resp = requests.get(url, params=params, headers=_NUPORT_HEADERS, timeout=30)
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


# ── WooCommerce helpers ───────────────────────────────────────────────────────

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


# ── Date helpers ──────────────────────────────────────────────────────────────

def _since_date_ymd() -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.LOOKBACK_DAYS)
    return cutoff.strftime("%Y-%m-%d")


def _since_date_iso() -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.LOOKBACK_DAYS)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S")


# ── Primary: Nuport flagged ───────────────────────────────────────────────────

def pull_flagged() -> dict[str, dict[str, int]]:
    """
    Pull Nuport flagged shipments (returns + COD refused) for last LOOKBACK_DAYS.

    Returns:
        {sku (uppercase) -> {"flagged_30d": int, "flagged_7d": int}}
        Empty dict on API failure.
    """
    logger.info("Pulling Nuport flagged data (returns + COD refused)...")

    try:
        flagged_shipments = _nuport_get(
            "shipments",
            {"from_date": _since_date_ymd(), "status": "flagged"},
        )
    except RuntimeError as exc:
        logger.error("Failed to pull Nuport flagged shipments: %s", exc)
        return {}

    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"flagged_30d": 0, "flagged_7d": 0})

    for shipment in flagged_shipments:
        date_str = (
            shipment.get("flagged_at")
            or shipment.get("updated_at")
            or shipment.get("created_at")
            or ""
        )
        try:
            shipment_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if shipment_date.tzinfo is None:
                shipment_date = shipment_date.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            shipment_date = None

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

            qty = int(item.get("quantity", 0))
            counts[sku]["flagged_30d"] += qty

            if shipment_date and shipment_date >= cutoff_7d:
                counts[sku]["flagged_7d"] += qty

    result = dict(counts)
    logger.info(
        "Nuport flagged: %d SKUs with return/refusal records.",
        len(result),
    )
    return result


# ── Fallback: WooCommerce refunded ────────────────────────────────────────────

def pull_wc_refunded() -> dict[str, dict[str, int]]:
    """
    Pull WooCommerce refunded orders for last LOOKBACK_DAYS as a return data fallback.

    Note: WC refunded data is 10-15% lower than Nuport flagged because
    WhatsApp and Messenger orders are not captured in WooCommerce.
    This is acceptable — the data is clean and SKU-accurate.

    Returns:
        {sku (uppercase) -> {"flagged_30d": int, "flagged_7d": int}}
        Empty dict on API failure.
    """
    logger.info("Pulling WooCommerce refunded orders as return data fallback...")

    try:
        refunded_orders = _wc_get(
            "orders",
            {
                "status": "refunded",
                "after": _since_date_iso(),
                "orderby": "date",
                "order": "desc",
            },
        )
    except RuntimeError as exc:
        logger.error("Failed to pull WooCommerce refunded orders: %s", exc)
        return {}

    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"flagged_30d": 0, "flagged_7d": 0})

    for order in refunded_orders:
        date_str = order.get("date_created", "")
        try:
            order_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if order_date.tzinfo is None:
                order_date = order_date.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            order_date = None

        for item in order.get("line_items", []):
            sku = (item.get("sku") or "").strip().upper()
            if not sku:
                product_id = item.get("product_id")
                if not product_id:
                    continue
                sku = f"WC-PRODUCT-{product_id}"

            qty = int(item.get("quantity", 0))
            counts[sku]["flagged_30d"] += qty

            if order_date and order_date >= cutoff_7d:
                counts[sku]["flagged_7d"] += qty

    result = dict(counts)
    logger.info(
        "WooCommerce refunded: %d SKUs with refund records "
        "(note: 10-15%% lower than Nuport flagged — WhatsApp/Messenger orders excluded).",
        len(result),
    )
    return result


# ── Orchestrator ──────────────────────────────────────────────────────────────

def pull_returns() -> tuple[dict[str, dict[str, int]], str]:
    """
    Pull return data, trying Nuport flagged first and falling back to WC refunded.

    Returns:
        (data, source_label)
        data:         {sku -> {"flagged_30d": int, "flagged_7d": int}}
        source_label: "nuport_flagged" | "wc_refunded" | "none"
    """
    # ── Try Nuport first ──────────────────────────────────────────────────────
    nuport_data = pull_flagged()
    if nuport_data:
        return nuport_data, "nuport_flagged"

    # ── Nuport returned nothing — try WooCommerce refunded ────────────────────
    logger.warning(
        "Nuport flagged returned no data. "
        "Falling back to WooCommerce refunded orders for return rate calculation."
    )
    wc_data = pull_wc_refunded()
    if wc_data:
        return wc_data, "wc_refunded"

    # ── Both sources empty ────────────────────────────────────────────────────
    logger.warning(
        "No return data available from either Nuport flagged or WooCommerce refunded. "
        "Per-SKU return rates will use RETURN_RATE_FALLBACK (%.0f%%) for all SKUs.",
        config.RETURN_RATE_FALLBACK * 100,
    )
    return {}, "none"
