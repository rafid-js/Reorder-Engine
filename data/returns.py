"""
Pull flagged shipments from Nuport for the last LOOKBACK_DAYS.

Nuport 'flagged' covers two distinct failure types:
  - Customer returns    (paid returns / exchange requested)
  - COD refused         (customer not available / rejected delivery)

Both reduce net revenue per unit and inflate effective return rate,
so both count in the numerator of per-SKU return rate calculations.

Returns per-SKU flagged quantity split into two windows:
  {sku (uppercase) -> {"flagged_30d": int, "flagged_7d": int}}

This data feeds engine/return_signals.py for per-SKU dynamic return rates.
"""

import time
from collections import defaultdict
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


def pull_flagged() -> dict[str, dict[str, int]]:
    """
    Pull Nuport flagged shipments (returns + COD refused) for last LOOKBACK_DAYS.

    Counts flagged quantity per SKU across two time windows:
      - flagged_30d: all flagged units in the lookback window
      - flagged_7d:  flagged units in the most recent 7 days (for early warning)

    Returns:
        {sku (uppercase) -> {"flagged_30d": int, "flagged_7d": int}}
        Empty dict if the API call fails (non-fatal; fallback rates apply downstream).
    """
    logger.info("Pulling Nuport flagged data (returns + COD refused)...")

    try:
        flagged_shipments = _nuport_get(
            "shipments",
            {"from_date": _since_date(), "status": "flagged"},
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
        "Nuport flagged data pulled. %d SKUs have return/refusal records "
        "(%d total flagged line-item records).",
        len(result),
        len(flagged_shipments),
    )
    return result
