"""
Send WhatsApp alerts via Twilio WhatsApp API.

Sends only when CRITICAL or WARNING SKUs exist, or return warnings exist.

Main alert format:
  ⚡ Winterfell Reorder Alert
  📅 [Date]
  🔴 Critical: N SKUs
  🟡 Warning: N SKUs
  Top urgent: ...
  → Full report in email + Sheets

If return warnings exist, appended section:
  ⚠️ Return Warnings: N SKUs spiking
  1. [Product] — X% → Y% this week
  → Details in email
"""

import time
from datetime import datetime

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

import config
from config import logger


def _is_configured() -> bool:
    return bool(config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and config.WHATSAPP_NUMBER)


def _get_client() -> Client:
    return Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)


def _build_kill_chain_block(dead_stock_skus: list[dict]) -> str:
    """Return kill chain alert lines, or empty string if no Liquidate/Bundle SKUs."""
    liquidate = [s for s in dead_stock_skus if s.get("kill_chain_stage") == "LIQUIDATE"]
    bundle    = [s for s in dead_stock_skus if s.get("kill_chain_stage") == "BUNDLE"]

    if not liquidate and not bundle:
        return ""

    lines = ["", "💀 *Kill Chain Alert*"]
    if liquidate:
        total_locked = sum(s.get("capital_locked", 0) for s in liquidate)
        lines.append(f"🔴 Liquidate: {len(liquidate)} SKUs — BDT {total_locked:,.0f} locked")
    if bundle:
        lines.append(f"🟠 Bundle: {len(bundle)} SKUs")
    lines.append("→ Exit strategy in email + Kill Chain sheet")
    return "\n".join(lines)


def _build_size_stockout_block(size_products: list[dict]) -> str:
    """Return the size stockout lines for WhatsApp message, or empty string if none."""
    stockouts = [
        (parent, size)
        for parent in size_products
        for size in parent["sizes"]
        if size["health_flag"] == "💀 SIZE_STOCKOUT"
    ]
    if not stockouts:
        return ""

    lines = ["", f"💀 *Size Stockouts Detected: {len(stockouts)} sizes out*"]
    for i, (parent, size) in enumerate(stockouts[:5], start=1):
        vel = size["net_velocity_14d"]
        lost_daily = round(vel, 1)
        lines.append(
            f"{i}. {parent['product_name']} — {size['size']} — "
            f"~{lost_daily}/day lost sales"
        )
    if len(stockouts) > 5:
        lines.append(f"  ...+{len(stockouts)-5} more — see Size Intelligence tab")
    return "\n".join(lines)


def _build_message(all_skus: list[dict], warning_skus: list[dict],
                   size_products: list[dict] | None = None,
                   dead_stock_skus: list[dict] | None = None) -> str:
    critical = [s for s in all_skus if s.get("urgency_tier") == "CRITICAL"]
    warning_tier = [s for s in all_skus if s.get("urgency_tier") == "WARNING"]

    top_urgent = sorted(
        critical + warning_tier,
        key=lambda s: s.get("days_remaining", 9999),
    )[:5]

    run_date = datetime.now().strftime("%d %b %Y")

    lines = [
        "⚡ *Winterfell Reorder Alert*",
        f"📅 {run_date}",
        f"🔴 Critical: {len(critical)} SKUs",
        f"🟡 Warning: {len(warning_tier)} SKUs",
        "",
        "Top urgent:",
    ]

    for i, s in enumerate(top_urgent, start=1):
        days = s.get("days_remaining", 9999)
        days_str = "OUT OF STOCK" if days <= 0 else f"{days}d left"
        lines.append(f"{i}. {s.get('sku','')} - {s.get('product_name','')} - {days_str}")

    lines.append("")
    lines.append("→ Full report in email + Sheets")

    # ── Return warnings block ─────────────────────────────────────────────────
    if warning_skus:
        lines.append("")
        lines.append(f"⚠️ *Return Warnings: {len(warning_skus)} SKUs spiking*")
        for i, s in enumerate(warning_skus[:3], start=1):
            rate_30d = s.get("return_rate_30d", 0)
            rate_7d = s.get("return_rate_7d", 0)
            lines.append(
                f"{i}. {s.get('product_name', s.get('sku',''))} — "
                f"{rate_30d*100:.0f}% → {rate_7d*100:.0f}% this week"
            )
        lines.append("→ Details in email")

    # ── Size stockout block ───────────────────────────────────────────────────
    size_block = _build_size_stockout_block(size_products or [])
    if size_block:
        lines.append(size_block)
        lines.append("→ Full size breakdown in Size Intelligence tab + email")

    # ── Kill chain block ──────────────────────────────────────────────────────
    kc_block = _build_kill_chain_block(dead_stock_skus or [])
    if kc_block:
        lines.append(kc_block)

    return "\n".join(lines)


def send_whatsapp_alert(
    all_skus: list[dict],
    warning_skus: list[dict] | None = None,
    size_products: list[dict] | None = None,
    dead_stock_skus: list[dict] | None = None,
) -> None:
    """
    Send WhatsApp alert if any CRITICAL/WARNING SKUs, return warnings, size stockouts,
    or kill chain Liquidate/Bundle SKUs exist. Silently skips if all clear.
    """
    warning_skus    = warning_skus or []
    size_products   = size_products or []
    dead_stock_skus = dead_stock_skus or []

    has_action = any(s.get("urgency_tier") in ("CRITICAL", "WARNING") for s in all_skus)
    has_warnings = bool(warning_skus)
    has_size_stockouts = any(
        s["health_flag"] == "💀 SIZE_STOCKOUT"
        for p in size_products for s in p["sizes"]
    )
    has_kill_chain = any(
        s.get("kill_chain_stage") in ("LIQUIDATE", "BUNDLE")
        for s in dead_stock_skus
    )

    if not _is_configured():
        logger.info("WhatsApp not configured — skipping alert.")
        return

    if not has_action and not has_warnings and not has_size_stockouts and not has_kill_chain:
        logger.info("All clear — WhatsApp alert skipped.")
        return

    message_body = _build_message(all_skus, warning_skus, size_products, dead_stock_skus)
    last_exc: Exception | None = None

    for attempt in range(1, config.MAX_API_RETRIES + 1):
        try:
            client = _get_client()
            message = client.messages.create(
                from_=config.TWILIO_WHATSAPP_FROM,
                to=f"whatsapp:{config.WHATSAPP_NUMBER}",
                body=message_body,
            )
            logger.info(
                "WhatsApp alert sent. SID: %s  To: %s",
                message.sid, config.WHATSAPP_NUMBER,
            )
            return
        except TwilioRestException as exc:
            last_exc = exc
            wait = config.RETRY_BACKOFF_BASE ** attempt
            logger.warning(
                "WhatsApp send attempt %d failed: %s — retrying in %ds",
                attempt, exc, wait,
            )
            time.sleep(wait)

    logger.error(
        "WhatsApp alert failed after %d retries: %s",
        config.MAX_API_RETRIES, last_exc,
    )
    raise RuntimeError(f"WhatsApp send failed: {last_exc}")


def send_error_alert(error_message: str) -> None:
    """Send a plain WhatsApp message if the entire run fails catastrophically."""
    if not _is_configured():
        return
    body = (
        "🚨 *Winterfell Reorder Engine — RUN FAILED*\n\n"
        f"Error: {error_message}\n\n"
        "Please check the logs immediately."
    )
    try:
        client = _get_client()
        client.messages.create(
            from_=config.TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:{config.WHATSAPP_NUMBER}",
            body=body,
        )
        logger.info("Error alert sent via WhatsApp.")
    except Exception as exc:
        logger.error("Failed to send error WhatsApp alert: %s", exc)
