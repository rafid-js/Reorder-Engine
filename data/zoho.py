"""
Pull purchase order data from Zoho Books API.

Provides per-SKU: supplier name, last purchase price (BDT), and MOQ if noted.
Used for cost estimation in reorder reports.

Zoho uses OAuth2 with a refresh token. We fetch a short-lived access token
before every run — no local token caching needed for a daily scheduler.
"""

import time
from typing import Any

import requests

import config
from config import logger

_ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
_ZOHO_BOOKS_BASE = "https://www.zohoapis.com/books/v3"


def _get_access_token() -> str:
    """Exchange refresh token for a short-lived access token."""
    last_exc: Exception | None = None

    for attempt in range(1, config.MAX_API_RETRIES + 1):
        try:
            resp = requests.post(
                _ZOHO_TOKEN_URL,
                data={
                    "refresh_token": config.ZOHO_REFRESH_TOKEN,
                    "client_id": config.ZOHO_CLIENT_ID,
                    "client_secret": config.ZOHO_CLIENT_SECRET,
                    "grant_type": "refresh_token",
                },
                timeout=20,
            )
            resp.raise_for_status()
            token = resp.json().get("access_token")
            if not token:
                raise ValueError(f"No access_token in response: {resp.json()}")
            return token
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            wait = config.RETRY_BACKOFF_BASE ** attempt
            logger.warning(
                "Zoho token refresh attempt %d failed: %s — retrying in %ds",
                attempt, exc, wait,
            )
            time.sleep(wait)

    raise RuntimeError(
        f"Zoho token refresh failed after {config.MAX_API_RETRIES} retries: {last_exc}"
    )


def _zoho_get(path: str, access_token: str, params: dict[str, Any] | None = None) -> list[dict]:
    """Paginated GET from Zoho Books API."""
    url = f"{_ZOHO_BOOKS_BASE}/{path.lstrip('/')}"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    params = params or {}
    params["organization_id"] = config.ZOHO_ORG_ID

    results: list[dict] = []
    page = 1

    while True:
        params["page"] = page
        last_exc: Exception | None = None

        for attempt in range(1, config.MAX_API_RETRIES + 1):
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.RequestException as exc:
                last_exc = exc
                wait = config.RETRY_BACKOFF_BASE ** attempt
                logger.warning(
                    "Zoho GET %s page %d attempt %d failed: %s — retrying in %ds",
                    path, page, attempt, exc, wait,
                )
                time.sleep(wait)
        else:
            raise RuntimeError(
                f"Zoho API failed after {config.MAX_API_RETRIES} retries: {last_exc}"
            )

        # Zoho wraps results in a key matching the resource type
        resource_key = path.strip("/").split("/")[0]  # e.g. "purchaseorders"
        batch = data.get(resource_key, [])
        if not batch:
            break

        results.extend(batch)

        page_context = data.get("page_context", {})
        if not page_context.get("has_more_page", False):
            break

        page += 1

    return results


def pull_purchase_orders() -> dict[str, dict]:
    """
    Pull all purchase orders from Zoho Books.

    Returns dict: {SKU (uppercase) -> {supplier_name, last_purchase_price, moq}}

    When multiple POs exist for a SKU, the most recent price is used.
    """
    logger.info("Pulling Zoho Books purchase order data...")

    try:
        access_token = _get_access_token()
    except RuntimeError as exc:
        logger.error("Zoho authentication failed: %s", exc)
        return {}

    try:
        purchase_orders = _zoho_get("purchaseorders", access_token)
    except RuntimeError as exc:
        logger.error("Failed to pull Zoho purchase orders: %s", exc)
        return {}

    # {sku -> {"supplier_name": str, "last_purchase_price": float, "moq": int, "date": str}}
    sku_data: dict[str, dict] = {}

    for po in purchase_orders:
        vendor_name = po.get("vendor_name", "")
        po_date = po.get("date", "")
        line_items = po.get("line_items", [])

        for item in line_items:
            sku = (item.get("sku") or "").strip().upper()
            if not sku:
                # Fall back to item name normalized if no SKU
                item_name = (item.get("name") or item.get("description") or "").strip()
                if not item_name:
                    continue
                sku = f"ZOHO-{item_name[:30].upper().replace(' ', '_')}"

            rate = float(item.get("rate", 0) or 0)
            quantity = int(item.get("quantity", 0) or 0)

            existing = sku_data.get(sku)
            if existing is None or po_date >= existing.get("date", ""):
                sku_data[sku] = {
                    "supplier_name": vendor_name,
                    "last_purchase_price": rate,
                    "moq": quantity,  # PO line qty used as a proxy for MOQ
                    "date": po_date,
                }

    logger.info("Zoho Books data pulled. %d SKUs with purchase order data.", len(sku_data))
    return sku_data
