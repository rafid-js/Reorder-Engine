"""
Write the Kill Chain tab to Google Sheets.

Tab: "Kill Chain"
- One row per dead stock SKU (score >= 50), sorted stage-desc then score-desc
- Color coded by stage: red / orange / yellow / grey
- Capital Recovery Summary section appended at the bottom after every run
"""

from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

import config
from config import logger
from engine.dead_stock import LIQUIDATE, BUNDLE, MARKDOWN, WATCH

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_HEADERS = [
    "SKU",
    "Product",
    "Size",
    "Dead Stock Score",
    "Stage",
    "Days Since Last Sale",
    "Sell-Through % (30d)",
    "Stock Age (days)",
    "Units Stuck",
    "Capital Locked (BDT)",
    "Suggested Action",
    "Discount %",
    "Claude Recommendation",
    "Status",
]

# Stage → row background color (RGB 0–1)
_STAGE_COLORS = {
    LIQUIDATE: {"red": 0.96, "green": 0.73, "blue": 0.73},   # light red
    BUNDLE:    {"red": 1.00, "green": 0.87, "blue": 0.70},   # light orange
    MARKDOWN:  {"red": 1.00, "green": 0.97, "blue": 0.80},   # light yellow
    WATCH:     {"red": 0.90, "green": 0.90, "blue": 0.90},   # light grey
}

_COLOR_HEADER      = {"red": 0.13, "green": 0.13, "blue": 0.13}
_COLOR_WHITE       = {"red": 1.00, "green": 1.00, "blue": 1.00}
_COLOR_SUMMARY_HDR = {"red": 0.20, "green": 0.20, "blue": 0.35}
_COLOR_SUMMARY_ROW = {"red": 0.93, "green": 0.93, "blue": 0.97}


def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(
        config.GOOGLE_SERVICE_ACCOUNT_JSON,
        scopes=_SCOPES,
    )
    return gspread.authorize(creds)


def _parse_size(sku: str) -> str:
    """Extract size token from sub-SKU, or empty string."""
    parts = sku.split("-")
    if len(parts) >= 2 and parts[-1].upper() in config.KNOWN_SIZES:
        return parts[-1].upper()
    return ""


def _suggested_action_text(sku_data: dict) -> str:
    stage = sku_data.get("kill_chain_stage")
    discount = sku_data.get("suggested_discount_pct", 0)
    if stage == MARKDOWN:
        return f"Apply {discount}% discount to clear in ~14 days"
    if stage == BUNDLE:
        bundle_with = sku_data.get("claude_bundle_with", "")
        return f"Bundle with fast mover{': ' + bundle_with if bundle_with else ''}"
    if stage == LIQUIDATE:
        recovery = sku_data.get("estimated_recovery", 0)
        return f"Wholesale clearance — est. BDT {recovery:,.0f} recovery at 60% cost"
    if stage == WATCH:
        return "Monitor — recheck in 7 days"
    return ""


def _data_row(sku_data: dict) -> list:
    stage = sku_data.get("kill_chain_stage", "")
    stage_label = sku_data.get("kill_chain_stage_label", stage)
    claude_rec = sku_data.get("claude_exit_action", "") or sku_data.get("claude_ops_instruction", "")

    return [
        sku_data.get("sku", ""),
        sku_data.get("product_name", ""),
        _parse_size(sku_data.get("sku", "")),
        sku_data.get("dead_stock_score", 0),
        stage_label,
        sku_data.get("days_since_last_sale", 0),
        sku_data.get("sell_through_30d_pct", 0),
        sku_data.get("stock_age_days", 0),
        sku_data.get("current_stock", 0),
        sku_data.get("capital_locked", 0.0),
        _suggested_action_text(sku_data),
        sku_data.get("suggested_discount_pct", 0) if stage == MARKDOWN else "",
        claude_rec,
        stage_label,
    ]


def _capital_recovery_summary(dead_stock_skus: list[dict]) -> list[list]:
    """Build the Capital Recovery Summary rows appended after data."""
    total_locked     = sum(s.get("capital_locked", 0) for s in dead_stock_skus)
    markdown_recovery = sum(
        s.get("estimated_recovery", 0) for s in dead_stock_skus if s.get("kill_chain_stage") == MARKDOWN
    )
    bundle_recovery   = sum(
        s.get("estimated_recovery", 0) for s in dead_stock_skus if s.get("kill_chain_stage") == BUNDLE
    )
    liquidate_recovery = sum(
        s.get("estimated_recovery", 0) for s in dead_stock_skus if s.get("kill_chain_stage") == LIQUIDATE
    )
    total_recovery = markdown_recovery + bundle_recovery + liquidate_recovery
    write_off_risk = sum(s.get("write_off_risk", 0) for s in dead_stock_skus)

    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    return [
        [],
        ["=== CAPITAL RECOVERY SUMMARY ===", "", f"Updated: {run_time}"],
        ["Total capital locked in dead stock (BDT):", f"{total_locked:,.0f}"],
        ["Est. recoverable via Markdown (BDT):",       f"{markdown_recovery:,.0f}"],
        ["Est. recoverable via Bundle (BDT):",          f"{bundle_recovery:,.0f}"],
        ["Est. recoverable via Liquidation (BDT):",     f"{liquidate_recovery:,.0f}"],
        ["Total est. recovery (BDT):",                  f"{total_recovery:,.0f}"],
        ["Est. write-off risk (BDT):",                  f"{write_off_risk:,.0f}"],
    ]


def write_kill_chain_sheet(dead_stock_skus: list[dict]) -> None:
    """
    Overwrite the Kill Chain tab with current dead stock data.

    Sort order: Liquidate → Bundle → Markdown → Watch, then score desc within stage.
    Appends Capital Recovery Summary after all rows.
    """
    logger.info("Writing Kill Chain tab to Google Sheets...")

    try:
        client = _get_client()
        spreadsheet = client.open_by_key(config.GOOGLE_SHEETS_ID)

        try:
            worksheet = spreadsheet.worksheet(config.SHEETS_KILL_CHAIN_TAB_NAME)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=config.SHEETS_KILL_CHAIN_TAB_NAME,
                rows=2000,
                cols=len(_HEADERS),
            )

        # Build rows
        rows: list[list] = [_HEADERS]
        data_row_meta: list[tuple[int, str]] = []  # (row_idx_0based, stage)

        for sku_data in dead_stock_skus:
            row_idx = len(rows)
            rows.append(_data_row(sku_data))
            data_row_meta.append((row_idx, sku_data.get("kill_chain_stage", WATCH)))

        summary_start = len(rows)
        rows.extend(_capital_recovery_summary(dead_stock_skus))

        worksheet.clear()
        worksheet.update("A1", rows)

        # ── Formatting ────────────────────────────────────────────────────────
        sheet_id = worksheet.id
        total_cols = len(_HEADERS)
        req: list[dict] = []

        # Header row
        req.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0, "endRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": total_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _COLOR_HEADER,
                        "textFormat": {"bold": True, "foregroundColor": _COLOR_WHITE},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        })

        # Data rows — stage color
        for row_0, stage in data_row_meta:
            color = _STAGE_COLORS.get(stage, _STAGE_COLORS[WATCH])
            req.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_0, "endRowIndex": row_0 + 1,
                        "startColumnIndex": 0, "endColumnIndex": total_cols,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": color}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

        # Capital Recovery Summary header row
        summary_title_row = summary_start + 1  # skip blank row
        req.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": summary_title_row,
                    "endRowIndex": summary_title_row + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": 3,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _COLOR_SUMMARY_HDR,
                        "textFormat": {"bold": True, "foregroundColor": _COLOR_WHITE},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        })

        # Summary data rows
        for i in range(6):  # 6 data rows in summary
            r = summary_title_row + 1 + i
            req.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": r, "endRowIndex": r + 1,
                        "startColumnIndex": 0, "endColumnIndex": 3,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": _COLOR_SUMMARY_ROW}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

        # Freeze header + auto-resize
        req.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        })
        req.append({
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0, "endIndex": total_cols,
                }
            }
        })

        spreadsheet.batch_update({"requests": req})

        total_locked = sum(s.get("capital_locked", 0) for s in dead_stock_skus)
        logger.info(
            "Kill Chain tab updated. %d dead stock SKUs | BDT %s locked.",
            len(dead_stock_skus), f"{total_locked:,.0f}",
        )

    except Exception as exc:
        logger.error("Kill Chain sheet write failed: %s", exc)
        raise
