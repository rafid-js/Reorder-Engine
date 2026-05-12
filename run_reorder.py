"""
Winterfell Reorder Engine — Entry Point

Usage:
  python run_reorder.py           # Start scheduler (runs daily at 10 AM BST)
  python run_reorder.py --now     # Run once immediately and exit
"""

import argparse
import sys
import traceback
from datetime import datetime

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler

import config
from config import logger

_BST = pytz.timezone(config.TIMEZONE)


def run_pipeline() -> None:
    """
    Full reorder pipeline:
      1. Pull data from WooCommerce, Nuport, Zoho Books
      2. Merge by SKU
      3. Compute velocity + urgency tiers
      4. Compute reorder quantities
      5. Get Claude AI recommendations
      6. Write to Google Sheets
      7. Send Gmail briefing
      8. Send WhatsApp alert (if RED/YELLOW SKUs exist)
    """
    start_time = datetime.now(_BST)
    logger.info("=" * 60)
    logger.info("Winterfell Reorder Engine starting — %s", start_time.strftime("%Y-%m-%d %H:%M %Z"))
    logger.info("=" * 60)

    # ── 1. Pull data ──────────────────────────────────────────────────────────
    wc_orders = []
    nuport_deliveries = []
    nuport_stock = {}
    zoho_pos = {}
    missing_sources = []

    print("Pulling WooCommerce data...", end=" ", flush=True)
    try:
        from data.woocommerce import pull_orders
        wc_orders = pull_orders()
        print(f"Done. {len({r['sku'] for r in wc_orders})} SKUs found.")
    except Exception as exc:
        print("FAILED.")
        logger.error("WooCommerce pull failed: %s", exc)
        missing_sources.append("WooCommerce")

    print("Pulling Nuport delivery data...", end=" ", flush=True)
    try:
        from data.nuport import pull_deliveries
        nuport_deliveries = pull_deliveries()
        print(f"Done. {len({r['sku'] for r in nuport_deliveries})} SKUs found.")
    except Exception as exc:
        print("FAILED.")
        logger.error("Nuport deliveries pull failed: %s", exc)
        missing_sources.append("Nuport (deliveries)")

    print("Pulling Nuport stock levels...", end=" ", flush=True)
    try:
        from data.nuport import pull_stock
        nuport_stock = pull_stock()
        print(f"Done. {len(nuport_stock)} SKUs found.")
    except Exception as exc:
        print("FAILED.")
        logger.error("Nuport stock pull failed: %s", exc)
        missing_sources.append("Nuport (stock)")

    print("Pulling Zoho Books data...", end=" ", flush=True)
    try:
        from data.zoho import pull_purchase_orders
        zoho_pos = pull_purchase_orders()
        print(f"Done. {len(zoho_pos)} SKUs found.")
    except Exception as exc:
        print("FAILED.")
        logger.error("Zoho Books pull failed: %s", exc)
        missing_sources.append("Zoho Books")

    if missing_sources:
        logger.warning(
            "Missing data sources (continuing with available data): %s",
            ", ".join(missing_sources),
        )

    # ── 2. Merge ──────────────────────────────────────────────────────────────
    print("Merging data sources...", end=" ", flush=True)
    from data.merger import merge
    merged = merge(wc_orders, nuport_deliveries, nuport_stock, zoho_pos)
    print(f"Done. {len(merged)} unique SKUs.")

    if not merged:
        logger.error("No SKU data available after merge. Aborting run.")
        _send_error_alert("No SKU data available — all data sources failed or returned empty.")
        return

    # ── 3. Velocity + urgency ─────────────────────────────────────────────────
    print("Computing velocity and urgency tiers...", end=" ", flush=True)
    from engine.velocity import compute_velocity
    enriched = compute_velocity(merged)
    print("Done.")

    # ── 4. Reorder quantities ─────────────────────────────────────────────────
    print("Computing reorder quantities...", end=" ", flush=True)
    from engine.reorder import compute_reorder_quantities
    enriched = compute_reorder_quantities(enriched)
    print("Done.")

    # ── 5. Claude AI recommendations (only for actionable SKUs) ───────────────
    from engine.velocity import filter_actionable
    actionable = filter_actionable(enriched)

    print(f"Getting Claude AI recommendations for {len(actionable)} actionable SKUs...", end=" ", flush=True)
    from engine.intelligence import get_recommendations, merge_recommendations
    recs = get_recommendations(actionable)
    if recs is None:
        print("FAILED — proceeding without AI recommendations.")
    else:
        print(f"Done. {len(recs)} recommendations received.")

    # Merge recommendations back into actionable list, then rebuild full list
    merge_recommendations(actionable, recs)

    # Healthy SKUs don't get AI recs — just add empty fields
    actionable_skus = {s["sku"] for s in actionable}
    for s in enriched:
        if s["sku"] not in actionable_skus:
            s.setdefault("claude_trend", "N/A")
            s.setdefault("claude_trend_note", "")
            s.setdefault("claude_recommended_qty", s.get("reorder_qty", 0))
            s.setdefault("claude_reasoning", "")
            s.setdefault("claude_risk_flag", "NONE")
            s.setdefault("claude_supplier_note", "")
            s.setdefault("claude_action_note", "")

    # ── 6. Google Sheets ──────────────────────────────────────────────────────
    print("Writing to Google Sheets...", end=" ", flush=True)
    try:
        from outputs.sheets import write_to_sheets
        write_to_sheets(enriched)
        print("Done.")
    except Exception as exc:
        print("FAILED.")
        logger.error("Sheets write failed (non-fatal): %s", exc)

    # ── 7. Email briefing ─────────────────────────────────────────────────────
    print("Sending email briefing...", end=" ", flush=True)
    try:
        from outputs.email import send_email
        send_email(enriched)
        print("Done.")
    except Exception as exc:
        print("FAILED.")
        logger.error("Email send failed (non-fatal): %s", exc)

    # ── 8. WhatsApp alert ─────────────────────────────────────────────────────
    print("Sending WhatsApp alert...", end=" ", flush=True)
    try:
        from outputs.whatsapp import send_whatsapp_alert
        send_whatsapp_alert(enriched)
        print("Done.")
    except Exception as exc:
        print("FAILED.")
        logger.error("WhatsApp alert failed (non-fatal): %s", exc)

    elapsed = (datetime.now(_BST) - start_time).total_seconds()
    logger.info(
        "Reorder Engine run complete in %.1fs. "
        "Critical: %d | Warning: %d | Healthy: %d",
        elapsed,
        sum(1 for s in enriched if s.get("urgency_tier") == "CRITICAL"),
        sum(1 for s in enriched if s.get("urgency_tier") == "WARNING"),
        sum(1 for s in enriched if s.get("urgency_tier") == "HEALTHY"),
    )
    logger.info("=" * 60)


def _send_error_alert(message: str) -> None:
    try:
        from outputs.whatsapp import send_error_alert
        send_error_alert(message)
    except Exception as exc:
        logger.error("Could not send error WhatsApp alert: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Winterfell Inventory Reorder Engine"
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Run the pipeline immediately and exit (no scheduler)",
    )
    args = parser.parse_args()

    if args.now:
        try:
            run_pipeline()
        except Exception as exc:
            tb = traceback.format_exc()
            logger.critical("Pipeline crashed: %s\n%s", exc, tb)
            _send_error_alert(str(exc)[:400])
            sys.exit(1)
        return

    # ── Scheduled mode: run daily at 10:00 AM BST ────────────────────────────
    scheduler = BlockingScheduler(timezone=_BST)
    scheduler.add_job(
        func=_safe_run,
        trigger="cron",
        hour=10,
        minute=0,
        id="daily_reorder",
        name="Winterfell Daily Reorder",
    )

    logger.info(
        "Scheduler started. Next run at 10:00 AM %s (%s).",
        config.TIMEZONE, _BST,
    )
    print(f"Scheduler running. Next execution: 10:00 AM {config.TIMEZONE} (BST UTC+6).")
    print("Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


def _safe_run() -> None:
    """Wrapper so scheduler continues even if pipeline raises."""
    try:
        run_pipeline()
    except Exception as exc:
        tb = traceback.format_exc()
        logger.critical("Scheduled pipeline crashed: %s\n%s", exc, tb)
        _send_error_alert(str(exc)[:400])


if __name__ == "__main__":
    main()
