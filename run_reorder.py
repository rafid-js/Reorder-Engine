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


def run_pipeline(no_ai: bool = False) -> None:
    """
    Full reorder pipeline:
      1.  Pull WooCommerce orders + product categories, Nuport shipments + flagged, Zoho Books
      2.  Merge all sources by SKU
      3.  Compute per-SKU return rates + early warning signals
      4.  Compute velocity (using per-SKU return rates)
      5.  Compute reorder quantities
      6.  Get Claude reorder recommendations (CRITICAL/WARNING SKUs)
      7.  Get Claude return analysis (early warning SKUs)
      8.  Compute size ratios (SS cutting ratio logic — no Claude)
      9.  Score dead stock (Kill Chain) + get Claude exit strategy
      10. Write to Google Sheets (Reorder Queue + Size Intelligence + Kill Chain)
      11. Send Gmail briefing (with return warnings + size section + kill chain report)
      12. Send WhatsApp alert (with size stockout + kill chain blocks)
    """
    start_time = datetime.now(_BST)
    logger.info("=" * 60)
    logger.info("Winterfell Reorder Engine starting — %s", start_time.strftime("%Y-%m-%d %H:%M %Z"))
    logger.info("=" * 60)

    # ── 1. Pull data ──────────────────────────────────────────────────────────
    wc_orders = []
    wc_all_skus: set[str] = set()
    wc_sku_size_map: dict[str, str] = {}
    nuport_shipments = []
    nuport_preorders = {}
    nuport_flagged = {}
    nuport_stock = {}
    zoho_pos = {}
    wc_categories = {}
    missing_sources = []

    print("Pulling WooCommerce data...", end=" ", flush=True)
    try:
        from data.woocommerce import pull_orders, pull_product_categories, pull_all_skus
        wc_orders = pull_orders()
        print(f"Done. {len({r['sku'] for r in wc_orders})} SKUs found.")
    except Exception as exc:
        print("FAILED.")
        logger.error("WooCommerce pull failed: %s", exc)
        missing_sources.append("WooCommerce")

    print("Pulling WooCommerce full product catalogue (parents + variations)...", end=" ", flush=True)
    try:
        wc_all_skus, wc_sku_size_map = pull_all_skus()
        print(f"Done. {len(wc_all_skus)} total SKUs, {len(wc_sku_size_map)} with size attributes.")
    except Exception as exc:
        print("FAILED (non-fatal — falling back to order-based SKUs).")
        logger.warning("WooCommerce full catalogue pull failed: %s", exc)
        wc_all_skus = set()
        wc_sku_size_map = {}

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
    merged = merge(wc_orders, nuport_shipments, nuport_preorders, nuport_stock, zoho_pos, wc_all_skus)
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

    from engine.intelligence import (
        get_recommendations, merge_recommendations,
        get_return_analysis, merge_return_analysis,
    )

    if no_ai:
        print(f"Skipping Claude reorder recommendations (--no-ai). {len(actionable)} actionable SKUs.")
        recs = []
    else:
        print(f"Getting Claude reorder recommendations for {len(actionable)} actionable SKUs...", end=" ", flush=True)
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

    if no_ai:
        print("Skipping Claude return analysis (--no-ai).")
    elif warning_skus_enriched:
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
    size_products = compute_size_ratios(enriched, wc_categories, wc_sku_size_map)
    stockout_count = sum(
        1 for p in size_products for s in p["sizes"] if s["health_flag"] == "💀 STOCKOUT"
    )
    print(f"Done. {len(size_products)} parent SKUs, {stockout_count} size stockouts.")

    # ── 9. Dead Stock Kill Chain scoring + Claude exit strategy ───────────────
    print("Scoring dead stock (Kill Chain)...", end=" ", flush=True)
    from engine.dead_stock import compute_dead_stock
    dead_stock_skus = compute_dead_stock(enriched)
    kc_blocked = sum(1 for s in dead_stock_skus if s.get("kill_chain_blocked"))
    print(
        f"Done. {len(dead_stock_skus)} flagged "
        f"(🔴 Liquidate: {sum(1 for s in dead_stock_skus if s.get('kill_chain_stage')=='LIQUIDATE')} | "
        f"🟠 Bundle: {sum(1 for s in dead_stock_skus if s.get('kill_chain_stage')=='BUNDLE')} | "
        f"🟡 Markdown: {sum(1 for s in dead_stock_skus if s.get('kill_chain_stage')=='MARKDOWN')} | "
        f"⚪ Watch: {sum(1 for s in dead_stock_skus if s.get('kill_chain_stage')=='WATCH')})"
    )

    if no_ai:
        print("Skipping Claude kill chain analysis (--no-ai).")
        from engine.intelligence import merge_kill_chain_analysis
        merge_kill_chain_analysis(dead_stock_skus, None)
    elif dead_stock_skus:
        # Fast movers = top healthy SKUs by velocity (for bundle pairing suggestions)
        fast_movers = sorted(
            [s for s in enriched if not s.get("kill_chain_stage")],
            key=lambda s: s.get("net_velocity_14d", 0),
            reverse=True,
        )[:10]

        print(f"Getting Claude kill chain analysis for {len(dead_stock_skus)} SKUs...", end=" ", flush=True)
        from engine.intelligence import get_kill_chain_analysis, merge_kill_chain_analysis
        kc_analysis = get_kill_chain_analysis(dead_stock_skus, fast_movers)
        if kc_analysis is None:
            print("FAILED — proceeding without kill chain analysis.")
        else:
            print(f"Done. {len(kc_analysis)} recommendations received.")
        merge_kill_chain_analysis(dead_stock_skus, kc_analysis)
    else:
        print("No dead stock detected — skipping kill chain analysis.")

    # Ensure all non-dead-stock enriched SKUs have default dead stock fields
    from engine.dead_stock import _stamp_defaults
    for s in enriched:
        if not s.get("kill_chain_stage") and "dead_stock_score" not in s:
            _stamp_defaults(s)

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

    print("Writing to Google Sheets (Kill Chain)...", end=" ", flush=True)
    try:
        from outputs.kill_chain_sheet import write_kill_chain_sheet
        write_kill_chain_sheet(dead_stock_skus)
        print("Done.")
    except Exception as exc:
        print("FAILED.")
        logger.error("Kill Chain sheet write failed (non-fatal): %s", exc)

    # ── 11. Email briefing ────────────────────────────────────────────────────
    print("Sending email briefing...", end=" ", flush=True)
    try:
        from outputs.email import send_email
        send_email(enriched, warning_skus_final, size_products, dead_stock_skus)
        print("Done.")
    except Exception as exc:
        print("FAILED.")
        logger.error("Email send failed (non-fatal): %s", exc)

    # ── 12. WhatsApp alert ────────────────────────────────────────────────────
    print("Sending WhatsApp alert...", end=" ", flush=True)
    try:
        from outputs.whatsapp import send_whatsapp_alert
        send_whatsapp_alert(enriched, warning_skus_final, size_products, dead_stock_skus)
        print("Done.")
    except Exception as exc:
        print("FAILED.")
        logger.error("WhatsApp alert failed (non-fatal): %s", exc)

    elapsed = (datetime.now(_BST) - start_time).total_seconds()
    logger.info(
        "Reorder Engine run complete in %.1fs. "
        "Critical: %d | Warning: %d | Healthy: %d | "
        "Return warnings: %d | Hold flags: %d | "
        "Size parents: %d | Size stockouts: %d | "
        "Dead stock: %d (blocked from reorder: %d)",
        elapsed,
        sum(1 for s in enriched if s.get("urgency_tier") == "CRITICAL"),
        sum(1 for s in enriched if s.get("urgency_tier") == "WARNING"),
        sum(1 for s in enriched if s.get("urgency_tier") == "HEALTHY"),
        len(warning_skus_final),
        sum(1 for s in enriched if s.get("hold_for_review")),
        len(size_products),
        sum(1 for p in size_products for s in p["sizes"] if s["health_flag"] == "💀 STOCKOUT"),
        len(dead_stock_skus),
        kc_blocked,
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
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Skip all Claude API calls (use formula-based results only). Free to run.",
    )
    args = parser.parse_args()

    if args.now:
        try:
            run_pipeline(no_ai=args.no_ai)
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


def _safe_run(no_ai: bool = False) -> None:
    """Wrapper so scheduler continues even if pipeline raises."""
    try:
        run_pipeline(no_ai=no_ai)
    except Exception as exc:
        tb = traceback.format_exc()
        logger.critical("Scheduled pipeline crashed: %s\n%s", exc, tb)
        _send_error_alert(str(exc)[:400])


if __name__ == "__main__":
    main()
