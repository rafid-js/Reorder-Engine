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
      1.  Pull WooCommerce orders + product categories, Nuport shipments + flagged, Zoho Books
      2.  Merge all sources by SKU
      3.  Compute per-SKU return rates + early warning signals
      4.  Compute velocity (using per-SKU return rates)
      5.  Compute reorder quantities
      6.  Get Claude reorder recommendations (CRITICAL/WARNING SKUs)
      7.  Get Claude return analysis (early warning SKUs)
      8.  Compute size ratios + get Claude size analysis
      9.  (reserved)
      10. Write to Google Sheets (Reorder Queue + Size Intelligence)
      11. Send Gmail briefing (with return warnings + size section)
      12. Send WhatsApp alert (with size stockout block)
    """
    start_time = datetime.now(_BST)
    logger.info("=" * 60)
    logger.info("Winterfell Reorder Engine starting — %s", start_time.strftime("%Y-%m-%d %H:%M %Z"))
    logger.info("=" * 60)

    # ── 1. Pull data ──────────────────────────────────────────────────────────
    wc_orders = []
    nuport_shipments = []
    nuport_preorders = {}
    nuport_flagged = {}
    nuport_stock = {}
    zoho_pos = {}
    wc_categories = {}
    missing_sources = []

    print("Pulling WooCommerce data...", end=" ", flush=True)
    try:
        from data.woocommerce import pull_orders, pull_product_categories
        wc_orders = pull_orders()
        print(f"Done. {len({r['sku'] for r in wc_orders})} SKUs found.")
    except Exception as exc:
        print("FAILED.")
        logger.error("WooCommerce pull failed: %s", exc)
        missing_sources.append("WooCommerce")

    print("Pulling WooCommerce product categories...", end=" ", flush=True)
    try:
        wc_categories = pull_product_categories()
        print(f"Done. {len(wc_categories)} parent SKUs mapped.")
    except Exception as exc:
        print("FAILED (non-fatal).")
        logger.warning("WooCommerce category pull failed: %s", exc)
        wc_categories = {}

    print("Pulling Nuport shipment data (pending/on-hold/in-transit/delivered)...", end=" ", flush=True)
    try:
        from data.nuport import pull_shipments
        nuport_shipments, nuport_preorders = pull_shipments()
        sku_count = len({r["sku"] for r in nuport_shipments})
        print(f"Done. {sku_count} SKUs in shipments, {len(nuport_preorders)} SKUs with pre-orders.")
    except Exception as exc:
        print("FAILED.")
        logger.error("Nuport shipments pull failed: %s", exc)
        missing_sources.append("Nuport (shipments)")

    print("Pulling return data (Nuport flagged → fallback: WC refunded)...", end=" ", flush=True)
    try:
        from data.returns import pull_returns
        nuport_flagged, return_source = pull_returns()
        source_note = {
            "nuport_flagged": "Nuport flagged",
            "wc_refunded":    "WooCommerce refunded (fallback — ~10-15% lower, WhatsApp/Messenger orders excluded)",
            "none":           "No source available — fallback rates will apply",
        }.get(return_source, return_source)
        print(f"Done. {len(nuport_flagged)} SKUs. Source: {source_note}.")
    except Exception as exc:
        print("FAILED.")
        logger.error("Return data pull failed: %s", exc)
        nuport_flagged = {}
        missing_sources.append("return data")

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
    merged = merge(wc_orders, nuport_shipments, nuport_preorders, nuport_stock, zoho_pos)
    print(f"Done. {len(merged)} unique SKUs.")

    if not merged:
        logger.error("No SKU data available after merge. Aborting run.")
        _send_error_alert("No SKU data available — all data sources failed or returned empty.")
        return

    # ── 3. Per-SKU return rates + early warning signals ───────────────────────
    print("Computing per-SKU return rates and early warning signals...", end=" ", flush=True)
    from engine.return_signals import compute_return_rates, get_warning_skus
    merged = compute_return_rates(merged, nuport_flagged)
    warning_skus = get_warning_skus(merged)
    hold_count = sum(1 for s in merged if s.get("hold_for_review"))
    print(f"Done. {len(warning_skus)} early warnings, {hold_count} hold flags.")

    # ── 4. Velocity + urgency (uses per-SKU return rates from step 3) ─────────
    print("Computing velocity and urgency tiers...", end=" ", flush=True)
    from engine.velocity import compute_velocity
    enriched = compute_velocity(merged)
    print("Done.")

    # ── 5. Reorder quantities ─────────────────────────────────────────────────
    print("Computing reorder quantities...", end=" ", flush=True)
    from engine.reorder import compute_reorder_quantities
    enriched = compute_reorder_quantities(enriched)
    print("Done.")

    # ── 6. Claude reorder recommendations ────────────────────────────────────
    from engine.velocity import filter_actionable
    actionable = filter_actionable(enriched)

    print(f"Getting Claude reorder recommendations for {len(actionable)} actionable SKUs...", end=" ", flush=True)
    from engine.intelligence import (
        get_recommendations, merge_recommendations,
        get_return_analysis, merge_return_analysis,
    )
    recs = get_recommendations(actionable)
    if recs is None:
        print("FAILED — proceeding without AI recommendations.")
    else:
        print(f"Done. {len(recs)} recommendations received.")
    merge_recommendations(actionable, recs)

    # Fill empty Claude fields for non-actionable (HEALTHY) SKUs
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

    # ── 7. Claude return analysis (early warning SKUs only) ───────────────────
    # Resolve warning_skus refs to enriched objects (which now have velocity data)
    enriched_map = {s["sku"]: s for s in enriched}
    warning_skus_enriched = [enriched_map[s["sku"]] for s in warning_skus if s["sku"] in enriched_map]

    if warning_skus_enriched:
        print(f"Getting Claude return analysis for {len(warning_skus_enriched)} warning SKUs...", end=" ", flush=True)
        return_analysis = get_return_analysis(warning_skus_enriched)
        if return_analysis is None:
            print("FAILED — proceeding without return analysis.")
        else:
            print(f"Done. {len(return_analysis)} analyses received.")
        merge_return_analysis(warning_skus_enriched, return_analysis)
    else:
        print("No return early warnings — skipping return analysis.")

    # Ensure all SKUs have return analysis placeholder fields
    for s in enriched:
        s.setdefault("claude_return_diagnosis", "N/A")
        s.setdefault("claude_return_analysis", "")
        s.setdefault("claude_return_hold", s.get("hold_for_review", False))
        s.setdefault("claude_return_investigate", False)
        s.setdefault("claude_return_action", "")

    # Final warning list with enriched data
    warning_skus_final = [s for s in enriched if s.get("return_early_warning")]

    # ── 8. Size Ratio Optimization Engine ─────────────────────────────────────
    print("Computing size ratios...", end=" ", flush=True)
    from engine.size_ratio import compute_size_ratios
    size_products = compute_size_ratios(enriched, wc_categories)
    stockout_count = sum(
        1 for p in size_products for s in p["sizes"] if s["health_flag"] == "💀 SIZE_STOCKOUT"
    )
    print(f"Done. {len(size_products)} parent SKUs, {stockout_count} size stockouts.")

    if size_products:
        print(f"Getting Claude size analysis for {len(size_products)} parent SKUs...", end=" ", flush=True)
        from engine.intelligence import get_size_analysis, merge_size_analysis
        size_analysis = get_size_analysis(size_products)
        if size_analysis is None:
            print("FAILED — proceeding without size analysis.")
        else:
            print(f"Done. {len(size_analysis)} analyses received.")
        merge_size_analysis(size_products, size_analysis)
    else:
        print("No multi-size parent SKUs found — skipping size analysis.")

    # ── 10. Google Sheets ─────────────────────────────────────────────────────
    print("Writing to Google Sheets (Reorder Queue)...", end=" ", flush=True)
    try:
        from outputs.sheets import write_to_sheets
        write_to_sheets(enriched)
        print("Done.")
    except Exception as exc:
        print("FAILED.")
        logger.error("Sheets write failed (non-fatal): %s", exc)

    if size_products:
        print("Writing to Google Sheets (Size Intelligence)...", end=" ", flush=True)
        try:
            from outputs.size_sheet import write_size_sheet
            write_size_sheet(size_products)
            print("Done.")
        except Exception as exc:
            print("FAILED.")
            logger.error("Size Intelligence sheet write failed (non-fatal): %s", exc)

    # ── 11. Email briefing ────────────────────────────────────────────────────
    print("Sending email briefing...", end=" ", flush=True)
    try:
        from outputs.email import send_email
        send_email(enriched, warning_skus_final, size_products)
        print("Done.")
    except Exception as exc:
        print("FAILED.")
        logger.error("Email send failed (non-fatal): %s", exc)

    # ── 12. WhatsApp alert ────────────────────────────────────────────────────
    print("Sending WhatsApp alert...", end=" ", flush=True)
    try:
        from outputs.whatsapp import send_whatsapp_alert
        send_whatsapp_alert(enriched, warning_skus_final, size_products)
        print("Done.")
    except Exception as exc:
        print("FAILED.")
        logger.error("WhatsApp alert failed (non-fatal): %s", exc)

    elapsed = (datetime.now(_BST) - start_time).total_seconds()
    logger.info(
        "Reorder Engine run complete in %.1fs. "
        "Critical: %d | Warning: %d | Healthy: %d | "
        "Return warnings: %d | Hold flags: %d | "
        "Size parents: %d | Size stockouts: %d",
        elapsed,
        sum(1 for s in enriched if s.get("urgency_tier") == "CRITICAL"),
        sum(1 for s in enriched if s.get("urgency_tier") == "WARNING"),
        sum(1 for s in enriched if s.get("urgency_tier") == "HEALTHY"),
        len(warning_skus_final),
        sum(1 for s in enriched if s.get("hold_for_review")),
        len(size_products),
        sum(1 for p in size_products for s in p["sizes"] if s["health_flag"] == "💀 SIZE_STOCKOUT"),
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
