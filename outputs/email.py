"""
Send the daily reorder briefing email via Gmail API.

Subject: "⚡ Winterfell Reorder Alert — [DATE] — [N] SKUs Need Action[ + [M] Return Warnings]"

Email sections:
  1. Summary counts
  2. 🔴 Critical SKUs table
  3. 🟡 Warning SKUs table
  4. ⚠️ Return Rate Early Warnings (only if spike SKUs exist)
  5. 📐 Size Ratio Recommendations (only if size products exist)
"""

import base64
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import config
from config import logger

_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def _get_gmail_service():
    creds = Credentials.from_service_account_file(
        config.GOOGLE_SERVICE_ACCOUNT_JSON,
        scopes=_SCOPES,
    )
    delegated = creds.with_subject(config.GMAIL_SENDER)
    return build("gmail", "v1", credentials=delegated, cache_discovery=False)


def _return_rate_badge(sku_data: dict) -> str:
    rate = sku_data.get("return_rate_30d", 0)
    pct = f"{rate * 100:.1f}%"
    if sku_data.get("hold_for_review"):
        return f'<span style="background:#c62828;color:#fff;padding:2px 6px;border-radius:3px;font-size:11px;">{pct} ⛔ HOLD</span>'
    if sku_data.get("high_return_risk"):
        return f'<span style="background:#e65100;color:#fff;padding:2px 6px;border-radius:3px;font-size:11px;">{pct} ⚠️</span>'
    return f'<span style="color:#555;">{pct}</span>'


def _sku_row_html(s: dict) -> str:
    days = s.get("days_remaining", 9999)
    days_str = "∞" if days >= 9999 else f"{days}d"
    rec = s.get("claude_action_note") or s.get("claude_trend_note") or "—"
    if s.get("hold_for_review"):
        rec = "⛔ HOLD — Human Review Required | " + rec

    return f"""
    <tr>
      <td style="padding:6px 10px;font-family:monospace;">{s.get('sku','')}</td>
      <td style="padding:6px 10px;">{s.get('product_name','')}</td>
      <td style="padding:6px 10px;text-align:center;">{s.get('current_stock',0)}</td>
      <td style="padding:6px 10px;text-align:center;">{round(s.get('raw_velocity_14d',0),2)}</td>
      <td style="padding:6px 10px;text-align:center;">{_return_rate_badge(s)}</td>
      <td style="padding:6px 10px;text-align:center;">{round(s.get('net_velocity_14d',0),2)}</td>
      <td style="padding:6px 10px;text-align:center;font-weight:bold;">{days_str}</td>
      <td style="padding:6px 10px;text-align:center;">{s.get('claude_recommended_qty') or s.get('reorder_qty',0)}</td>
      <td style="padding:6px 10px;text-align:right;">BDT {s.get('estimated_cost',0):,.0f}</td>
      <td style="padding:6px 10px;">{rec}</td>
    </tr>"""


def _build_sku_table(skus: list[dict]) -> str:
    if not skus:
        return "<p><em>None</em></p>"
    rows_html = "".join(_sku_row_html(s) for s in skus)
    return f"""
    <table border="0" cellspacing="0" cellpadding="0"
           style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="background:#333;color:#fff;">
          <th style="padding:8px 10px;text-align:left;">SKU</th>
          <th style="padding:8px 10px;text-align:left;">Product</th>
          <th style="padding:8px 10px;text-align:center;">Stock</th>
          <th style="padding:8px 10px;text-align:center;">Gross Vel.</th>
          <th style="padding:8px 10px;text-align:center;">Return Rate</th>
          <th style="padding:8px 10px;text-align:center;">Net Vel.</th>
          <th style="padding:8px 10px;text-align:center;">Days Left</th>
          <th style="padding:8px 10px;text-align:center;">Order Qty</th>
          <th style="padding:8px 10px;text-align:right;">Est. Cost</th>
          <th style="padding:8px 10px;text-align:left;">Recommendation</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>"""


def _build_return_warnings_section(warning_skus: list[dict]) -> str:
    """HTML section for ⚠️ Return Rate Early Warnings."""
    if not warning_skus:
        return ""

    rows = []
    for s in warning_skus:
        rate_30d = s.get("return_rate_30d", 0)
        rate_7d = s.get("return_rate_7d", 0)
        spike = (rate_7d - rate_30d) * 100
        diagnosis = s.get("claude_return_diagnosis", "UNKNOWN").replace("_", " ").title()
        analysis = s.get("claude_return_analysis", "")
        action = s.get("claude_return_action", "")
        investigate = s.get("claude_return_investigate", False)
        hold = s.get("claude_return_hold", False)

        flags = []
        if hold:
            flags.append('<span style="background:#c62828;color:#fff;padding:1px 5px;border-radius:3px;font-size:11px;">⛔ Hold Reorder</span>')
        if investigate:
            flags.append('<span style="background:#e65100;color:#fff;padding:1px 5px;border-radius:3px;font-size:11px;">🔍 Investigate Supplier</span>')
        flags_html = " ".join(flags) if flags else ""

        rows.append(f"""
        <tr style="border-bottom:1px solid #eee;">
          <td style="padding:10px;font-family:monospace;font-weight:bold;">{s.get('sku','')}</td>
          <td style="padding:10px;">{s.get('product_name','')}</td>
          <td style="padding:10px;text-align:center;">
            <span style="color:#555;">{rate_30d*100:.1f}%</span>
            &nbsp;→&nbsp;
            <span style="color:#c62828;font-weight:bold;">{rate_7d*100:.1f}%</span>
            <br/><span style="color:#e65100;font-size:11px;">+{spike:.1f}pp spike</span>
          </td>
          <td style="padding:10px;">
            <strong>{diagnosis}</strong><br/>
            <span style="font-size:12px;color:#555;">{analysis}</span>
          </td>
          <td style="padding:10px;">
            {flags_html}<br/>
            <span style="font-size:12px;">{action}</span>
          </td>
        </tr>""")

    rows_html = "".join(rows)
    return f"""
    <div style="margin-top:30px;">
      <div style="background:#fff3e0;border-left:4px solid #e65100;padding:12px 20px;margin-bottom:12px;">
        <h2 style="margin:0;color:#bf360c;font-size:18px;">⚠️ Return Rate Early Warnings</h2>
        <p style="margin:4px 0 0;color:#555;font-size:13px;">
          These SKUs show a sharp return rate spike this week vs their 30-day baseline.
          Possible causes: quality issue, sizing problem, or supplier batch defect.
        </p>
      </div>
      <table border="0" cellspacing="0" cellpadding="0"
             style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead>
          <tr style="background:#bf360c;color:#fff;">
            <th style="padding:8px 10px;text-align:left;">SKU</th>
            <th style="padding:8px 10px;text-align:left;">Product</th>
            <th style="padding:8px 10px;text-align:center;">30d Rate → 7d Rate</th>
            <th style="padding:8px 10px;text-align:left;">Claude Diagnosis</th>
            <th style="padding:8px 10px;text-align:left;">Action</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>"""


def _build_size_section(size_products: list[dict]) -> str:
    """HTML section for 📐 Size Ratio Recommendations."""
    if not size_products:
        return ""

    product_blocks = []
    for parent in size_products:
        sizes = parent.get("sizes", [])
        top_action = parent.get("claude_top_action", "")
        risk_summary = parent.get("claude_risk_summary", "")
        verdict = parent.get("claude_ratio_verdict", "")

        verdict_badge = ""
        if verdict == "OPTIMIZE":
            verdict_badge = '<span style="background:#e65100;color:#fff;padding:2px 6px;border-radius:3px;font-size:11px;margin-left:8px;">OPTIMIZE RATIO</span>'
        elif verdict == "MAINTAIN":
            verdict_badge = '<span style="background:#2e7d32;color:#fff;padding:2px 6px;border-radius:3px;font-size:11px;margin-left:8px;">MAINTAIN RATIO</span>'

        size_rows = []
        for s in sizes:
            flag = s["health_flag"]
            flag_color = {
                "💀 SIZE_STOCKOUT": "#c62828",
                "🔥 FAST_MOVER": "#e65100",
                "🧊 SLOW_MOVER": "#1565c0",
                "⚠️ OVERSTOCK_RISK": "#f9a825",
            }.get(flag, "#555")

            change = s.get("change_vs_last")
            change_str = ""
            if change is not None:
                color = "#2e7d32" if change >= 0 else "#c62828"
                prefix = "+" if change >= 0 else ""
                change_str = f'<span style="color:{color};font-size:11px;">{prefix}{change}</span>'

            note = s.get("claude_size_note", "")
            size_rows.append(f"""
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:6px 10px;font-weight:bold;text-align:center;">{s['size']}</td>
              <td style="padding:6px 10px;font-family:monospace;font-size:12px;">{s['sku']}</td>
              <td style="padding:6px 10px;text-align:center;">{s['current_stock']}</td>
              <td style="padding:6px 10px;text-align:center;">{s['net_velocity_14d']}</td>
              <td style="padding:6px 10px;text-align:center;">{s['size_ratio_pct']}%</td>
              <td style="padding:6px 10px;text-align:center;">{s['sell_through_pct']}%</td>
              <td style="padding:6px 10px;text-align:center;">{'OUT' if s['days_remaining'] <= 0 else s['days_remaining']}</td>
              <td style="padding:6px 10px;text-align:center;font-weight:bold;">{s['suggested_qty']} {change_str}</td>
              <td style="padding:6px 10px;color:{flag_color};font-weight:bold;">{flag}</td>
              <td style="padding:6px 10px;font-size:12px;color:#555;">{note}</td>
            </tr>""")

        size_rows_html = "".join(size_rows)

        product_blocks.append(f"""
        <div style="margin-bottom:28px;border:1px solid #e0e0e0;border-radius:6px;overflow:hidden;">
          <div style="background:#e8eaf6;padding:10px 16px;border-bottom:1px solid #c5cae9;">
            <strong style="font-size:14px;">{parent['parent_sku']}</strong>
            <span style="color:#555;margin-left:8px;">{parent['product_name']}</span>
            <span style="color:#888;margin-left:8px;font-size:12px;">[{parent['category']}]</span>
            {verdict_badge}
            <span style="float:right;font-size:12px;color:#555;">Total order: <strong>{parent['total_reorder_qty']}</strong> units</span>
          </div>
          <table border="0" cellspacing="0" cellpadding="0"
                 style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead>
              <tr style="background:#3949ab;color:#fff;">
                <th style="padding:7px 10px;text-align:center;">Size</th>
                <th style="padding:7px 10px;text-align:left;">Sub-SKU</th>
                <th style="padding:7px 10px;text-align:center;">Stock</th>
                <th style="padding:7px 10px;text-align:center;">Net Vel.</th>
                <th style="padding:7px 10px;text-align:center;">Ratio %</th>
                <th style="padding:7px 10px;text-align:center;">Sell-Through</th>
                <th style="padding:7px 10px;text-align:center;">Days Left</th>
                <th style="padding:7px 10px;text-align:center;">Suggested Qty</th>
                <th style="padding:7px 10px;text-align:left;">Health</th>
                <th style="padding:7px 10px;text-align:left;">Notes</th>
              </tr>
            </thead>
            <tbody>{size_rows_html}</tbody>
          </table>
          {"" if not (top_action or risk_summary) else f'''
          <div style="background:#f5f5f5;padding:8px 16px;border-top:1px solid #e0e0e0;font-size:12px;">
            {"" if not top_action else f"<strong>Action:</strong> {top_action}&nbsp;&nbsp;"}
            {"" if not risk_summary else f"<strong>Risk:</strong> {risk_summary}"}
          </div>'''}
        </div>""")

    blocks_html = "".join(product_blocks)
    return f"""
    <div style="margin-top:30px;">
      <div style="background:#e8eaf6;border-left:4px solid #3949ab;padding:12px 20px;margin-bottom:16px;">
        <h2 style="margin:0;color:#1a237e;font-size:18px;">📐 Size Ratio Recommendations</h2>
        <p style="margin:4px 0 0;color:#555;font-size:13px;">
          Per-size velocity analysis and optimized production quantities.
          💀 stockouts and 🔥 fast movers need immediate attention.
        </p>
      </div>
      {blocks_html}
    </div>"""


def _build_kill_chain_section(dead_stock_skus: list[dict]) -> str:
    """HTML section for 💀 Kill Chain Report. Only shown if Stage 2+ SKUs exist."""
    from engine.dead_stock import LIQUIDATE, BUNDLE, MARKDOWN

    actionable = [
        s for s in dead_stock_skus
        if s.get("kill_chain_stage") in (LIQUIDATE, BUNDLE, MARKDOWN)
    ]
    if not actionable:
        return ""

    liquidate = [s for s in actionable if s["kill_chain_stage"] == LIQUIDATE]
    bundle    = [s for s in actionable if s["kill_chain_stage"] == BUNDLE]
    markdown  = [s for s in actionable if s["kill_chain_stage"] == MARKDOWN]

    total_locked   = sum(s.get("capital_locked", 0) for s in actionable)
    total_recovery = sum(s.get("estimated_recovery", 0) for s in actionable)

    def _stage_rows(skus: list[dict], stage_color: str, stage_emoji: str) -> str:
        rows = []
        for s in skus:
            rec = s.get("claude_exit_action") or s.get("claude_ops_instruction") or "—"
            brand_risk = s.get("claude_brand_risk", "")
            risk_badge = ""
            if brand_risk == "High":
                risk_badge = '<span style="background:#c62828;color:#fff;padding:1px 5px;border-radius:3px;font-size:11px;margin-left:6px;">High Risk</span>'
            elif brand_risk == "Medium":
                risk_badge = '<span style="background:#e65100;color:#fff;padding:1px 5px;border-radius:3px;font-size:11px;margin-left:6px;">Med Risk</span>'

            bundle_note = ""
            if s.get("claude_bundle_with"):
                bundle_note = f'<br/><span style="font-size:12px;color:#555;">Pair with: <strong>{s["claude_bundle_with"]}</strong></span>'

            discount_note = ""
            if s.get("kill_chain_stage") == MARKDOWN and s.get("suggested_discount_pct"):
                discount_note = f'<br/><span style="font-size:12px;color:#555;">Suggested discount: <strong>{s["suggested_discount_pct"]}%</strong> — est. clearance in 14 days</span>'

            rows.append(f"""
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:8px 10px;font-family:monospace;font-weight:bold;">{s.get('sku','')}</td>
              <td style="padding:8px 10px;">{s.get('product_name','')}</td>
              <td style="padding:8px 10px;text-align:center;">{s.get('current_stock',0)} pcs</td>
              <td style="padding:8px 10px;text-align:right;">BDT {s.get('capital_locked',0):,.0f}</td>
              <td style="padding:8px 10px;">
                {rec}{risk_badge}{bundle_note}{discount_note}
              </td>
            </tr>""")
        return "".join(rows)

    def _stage_block(skus: list[dict], title: str, header_bg: str) -> str:
        if not skus:
            return ""
        rows_html = _stage_rows(skus, header_bg, "")
        return f"""
        <div style="margin-bottom:20px;">
          <div style="background:{header_bg};color:#fff;padding:8px 14px;border-radius:4px 4px 0 0;font-weight:bold;font-size:14px;">{title} ({len(skus)} SKUs)</div>
          <table border="0" cellspacing="0" cellpadding="0"
                 style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #ddd;border-top:none;">
            <thead>
              <tr style="background:#f5f5f5;">
                <th style="padding:7px 10px;text-align:left;">SKU</th>
                <th style="padding:7px 10px;text-align:left;">Product</th>
                <th style="padding:7px 10px;text-align:center;">Units</th>
                <th style="padding:7px 10px;text-align:right;">Capital Locked</th>
                <th style="padding:7px 10px;text-align:left;">Action</th>
              </tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>"""

    liquidate_block = _stage_block(liquidate, "🔴 LIQUIDATE", "#b71c1c")
    bundle_block    = _stage_block(bundle,    "🟠 BUNDLE",    "#e65100")
    markdown_block  = _stage_block(markdown,  "🟡 MARKDOWN",  "#f9a825")

    return f"""
    <div style="margin-top:30px;">
      <div style="background:#1a1a2e;border-left:4px solid #c62828;padding:14px 20px;margin-bottom:16px;">
        <h2 style="margin:0;color:#fff;font-size:18px;">💀 Kill Chain Report — {len(actionable)} SKUs Need Exit Action</h2>
        <p style="margin:6px 0 0;color:#ccc;font-size:13px;">
          Total capital locked: <strong>BDT {total_locked:,.0f}</strong>
          &nbsp;|&nbsp; Est. recovery possible: <strong>BDT {total_recovery:,.0f}</strong>
        </p>
      </div>
      {liquidate_block}
      {bundle_block}
      {markdown_block}
    </div>"""


def build_email_html(all_skus: list[dict], warning_skus: list[dict], run_date: str,
                     size_products: list[dict] | None = None,
                     dead_stock_skus: list[dict] | None = None) -> str:
    critical = [s for s in all_skus if s.get("urgency_tier") == "CRITICAL"]
    warning = [s for s in all_skus if s.get("urgency_tier") == "WARNING"]
    healthy = [s for s in all_skus if s.get("urgency_tier") == "HEALTHY"]

    total_cost = sum(
        s.get("estimated_cost", 0)
        for s in all_skus
        if s.get("urgency_tier") in ("CRITICAL", "WARNING")
    )

    return_warnings_html = _build_return_warnings_section(warning_skus)
    size_section_html = _build_size_section(size_products or [])
    kill_chain_html = _build_kill_chain_section(dead_stock_skus or [])

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;max-width:1100px;margin:0 auto;color:#222;">
  <div style="background:#1a1a2e;color:#fff;padding:20px 30px;border-radius:8px 8px 0 0;">
    <h1 style="margin:0;font-size:22px;">⚡ Winterfell Reorder Alert</h1>
    <p style="margin:4px 0 0;opacity:0.8;">{run_date} &nbsp;|&nbsp; Auto-generated by Winterfell Reorder Engine</p>
  </div>

  <div style="background:#f5f5f5;padding:16px 30px;border-bottom:1px solid #ddd;">
    <strong>Summary:</strong>
    &nbsp; 🔴 Critical: <strong>{len(critical)}</strong>
    &nbsp;&nbsp; 🟡 Warning: <strong>{len(warning)}</strong>
    &nbsp;&nbsp; 🟢 Healthy: <strong>{len(healthy)}</strong>
    &nbsp;&nbsp; ⚠️ Return Warnings: <strong>{len(warning_skus)}</strong>
    &nbsp;&nbsp; 💀 Kill Chain: <strong>{len([s for s in (dead_stock_skus or []) if s.get("kill_chain_stage") in ("MARKDOWN","BUNDLE","LIQUIDATE")])}</strong>
    &nbsp;&nbsp;&nbsp; | &nbsp;
    Estimated reorder cost (RED+YELLOW): <strong>BDT {total_cost:,.0f}</strong>
  </div>

  <div style="padding:20px 30px;">

    <h2 style="color:#c62828;">🔴 Critical — Immediate Action Required ({len(critical)} SKUs)</h2>
    {_build_sku_table(critical)}

    <br/>
    <h2 style="color:#f9a825;">🟡 Warning — Order This Week ({len(warning)} SKUs)</h2>
    {_build_sku_table(warning)}

    {return_warnings_html}

    {size_section_html}

    {kill_chain_html}

  </div>

  <div style="background:#f0f0f0;padding:14px 30px;border-top:1px solid #ddd;
              font-size:12px;color:#666;border-radius:0 0 8px 8px;">
    Auto-generated by Winterfell Reorder Engine &nbsp;|&nbsp;
    Data sources: WooCommerce · Nuport · Zoho Books &nbsp;|&nbsp;
    Intelligence: Claude ({config.CLAUDE_MODEL})
  </div>
</body>
</html>"""


def send_email(
    all_skus: list[dict],
    warning_skus: list[dict] | None = None,
    size_products: list[dict] | None = None,
    dead_stock_skus: list[dict] | None = None,
) -> None:
    """Build and send the daily reorder briefing email."""
    warning_skus    = warning_skus or []
    size_products   = size_products or []
    dead_stock_skus = dead_stock_skus or []

    critical_count = sum(1 for s in all_skus if s.get("urgency_tier") == "CRITICAL")
    warning_count  = sum(1 for s in all_skus if s.get("urgency_tier") == "WARNING")
    action_count   = critical_count + warning_count

    run_date = datetime.now().strftime("%d %b %Y")

    subject = f"⚡ Winterfell Reorder Alert — {run_date} — {action_count} SKUs Need Action"
    if warning_skus:
        subject += f" + {len(warning_skus)} Return Warnings"
    if size_products:
        stockout_count = sum(
            1 for p in size_products for s in p["sizes"] if s["health_flag"] == "💀 SIZE_STOCKOUT"
        )
        if stockout_count:
            subject += f" + {stockout_count} Size Stockouts"
    kc_actionable = [
        s for s in dead_stock_skus
        if s.get("kill_chain_stage") in ("MARKDOWN", "BUNDLE", "LIQUIDATE")
    ]
    if kc_actionable:
        subject += f" + {len(kc_actionable)} Kill Chain"

    html_body = build_email_html(all_skus, warning_skus, run_date, size_products, dead_stock_skus)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.GMAIL_SENDER
    msg["To"] = config.REORDER_EMAIL
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    try:
        service = _get_gmail_service()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info("Email sent to %s. Subject: %s", config.REORDER_EMAIL, subject)
    except Exception as exc:
        logger.error("Gmail send failed: %s", exc)
        raise
