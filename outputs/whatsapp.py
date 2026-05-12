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


def _get_client() -> Client:
    return Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)


def _build_message(all_skus: list[dict], warning_skus: list[dict]) -> str:
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

    return "\n".join(lines)


def send_whatsapp_alert(all_skus: list[dict], warning_skus: list[dict] | None = None) -> None:
    """
    Send WhatsApp alert if any CRITICAL/WARNING SKUs or return warnings exist.
    Silently skips if everything is HEALTHY and no warnings.
    """
    warning_skus = warning_skus or []

    has_action = any(s.get("urgency_tier") in ("CRITICAL", "WARNING") for s in all_skus)
    has_warnings = bool(warning_skus)

    if not has_action and not has_warnings:
        logger.info("No CRITICAL/WARNING SKUs and no return warnings — WhatsApp alert skipped.")
        return

    message_body = _build_message(all_skus, warning_skus)
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
