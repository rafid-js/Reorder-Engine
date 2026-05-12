"""
Send WhatsApp alerts via Twilio WhatsApp API.

Sends only when CRITICAL or WARNING SKUs exist.
Message is concise — full details are in email + Sheets.

Format:
⚡ *Winterfell Reorder Alert*
📅 [Date]
🔴 Critical: [N] SKUs
🟡 Warning: [N] SKUs

Top urgent:
1. [SKU] - [Product] - [X] days left
2. ...

→ Full report in email + Sheets
"""

import time
from datetime import datetime

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

import config
from config import logger


def _get_client() -> Client:
    return Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)


def _build_message(all_skus: list[dict]) -> str:
    critical = [s for s in all_skus if s.get("urgency_tier") == "CRITICAL"]
    warning = [s for s in all_skus if s.get("urgency_tier") == "WARNING"]

    # Top 5 most urgent (by days_remaining ascending)
    top_urgent = sorted(
        critical + warning,
        key=lambda s: s.get("days_remaining", 9999),
    )[:5]

    run_date = datetime.now().strftime("%d %b %Y")

    lines = [
        "⚡ *Winterfell Reorder Alert*",
        f"📅 {run_date}",
        f"🔴 Critical: {len(critical)} SKUs",
        f"🟡 Warning: {len(warning)} SKUs",
        "",
        "Top urgent:",
    ]

    for i, s in enumerate(top_urgent, start=1):
        days = s.get("days_remaining", 9999)
        days_str = "OUT OF STOCK" if days <= 0 else f"{days}d left"
        lines.append(f"{i}. {s.get('sku','')} - {s.get('product_name','')} - {days_str}")

    lines.append("")
    lines.append("→ Full report in email + Sheets")

    return "\n".join(lines)


def send_whatsapp_alert(all_skus: list[dict]) -> None:
    """
    Send WhatsApp alert if any CRITICAL or WARNING SKUs exist.
    Silently skips if all SKUs are HEALTHY.
    """
    has_action = any(
        s.get("urgency_tier") in ("CRITICAL", "WARNING") for s in all_skus
    )

    if not has_action:
        logger.info("No CRITICAL/WARNING SKUs — WhatsApp alert skipped.")
        return

    message_body = _build_message(all_skus)
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
