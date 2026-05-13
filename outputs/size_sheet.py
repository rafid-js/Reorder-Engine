"""
Write the Size Intelligence tab to Google Sheets.

Tab: SHEETS_SIZE_TAB_NAME ("Size Intelligence")
- One section per parent SKU, each with per-size breakdown rows
- Health flag color coding per row
- Production Brief section at bottom (copy-paste ready for production manager)
- Overwrites entire tab on every run
"""

from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

import config
from config import logger

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_HEADERS = [
    "Parent SKU",
    "Product Name",
    "Category",
    "Size",
    "Sales (30d)",
    "Size Ratio %",
    "Current Stock",
    "Days Remaining",
    "Suggested Order Qty",
    "Change vs Last",
    "Health",
]

# Health flag → background color (RGB 0-1 scale)
_FLAG_COLORS: dict[str, dict] = {
    "💀 SIZE_STOCKOUT":  {"red": 0.90, "green": 0.20, "blue": 0.20},
    "🔥 FAST_MOVER":     {"red": 1.00, "green": 0.85, "blue": 0.60},
    "🧊 SLOW_MOVER":     {"red": 0.75, "green": 0.87, "blue": 0.95},
    "⚠️ OVERSTOCK_RISK": {"red": 1.00, "green": 0.95, "blue": 0.70},
    "OK":                {"red": 0.85, "green": 0.93, "blue": 0.83},
}

_COLOR_HEADER   = {"red": 0.23, "green": 0.23, "blue": 0.23}
_COLOR_WHITE    = {"red": 1.00, "green": 1.00, "blue": 1.00}
_COLOR_SECTION  = {"red": 0.93, "green": 0.93, "blue": 0.93}  # parent SKU group header
_COLOR_BRIEF_BG = {"red": 0.95, "green": 0.95, "blue": 1.00}  # production brief header


def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(
        config.GOOGLE_SERVICE_ACCOUNT_JSON,
        scopes=_SCOPES,
    )
    return gspread.authorize(creds)


def _size_row(parent: dict, size: dict) -> list:
    change = size.get("change_vs_last")
    change_str = "" if change is None else (f"+{change}" if change >= 0 else str(change))
    days = size.get("days_remaining", 9999)
    days_display = "OUT" if days <= 0 else ("∞" if days >= 9999 else days)

    return [
        parent["parent_sku"],
        parent["product_name"],
        parent["category"],
        size["size"],
        size.get("total_ordered_30d", 0),
        size["size_ratio_pct"],
        size["current_stock"],
        days_display,
        size["suggested_qty"],
        change_str,
        size["health_flag"],
    ]


def _production_brief_rows(size_products: list[dict]) -> list[list]:
    """Production Brief — one row per product showing size ratio breakdown."""
    rows: list[list] = [
        [],
        [f"=== PRODUCTION BRIEF — {datetime.now().strftime('%Y-%m-%d %H:%M')} ==="],
        [],
        ["Parent SKU", "Product", "Category", "Total Order Qty", "Size Ratio Breakdown"],
    ]
    for parent in size_products:
        if not parent.get("total_reorder_qty"):
            continue
        total_vel = parent.get("total_velocity", 0) or 1
        parts = []
        for size in parent["sizes"]:
            qty = size["suggested_qty"]
            ratio = size["size_ratio_pct"]
            flag = size["health_flag"]
            icon = "💀" if "STOCKOUT" in flag else ("🔥" if "FAST" in flag else ("🧊" if "SLOW" in flag else ""))
            parts.append(f"{size['size']}: {qty} ({ratio}%){icon}")
        rows.append([
            parent["parent_sku"],
            parent["product_name"],
            parent["category"],
            parent["total_reorder_qty"],
            "  |  ".join(parts),
        ])
    return rows


def write_size_sheet(size_products: list[dict]) -> None:
    """
    Overwrite the Size Intelligence tab with current size ratio data.

    Layout:
      Row 1: headers
      Rows 2–N: one row per size variant, grouped by parent SKU
      After all data: blank row + Production Brief section
    """
    if not size_products:
        logger.info("No size products to write — Size Intelligence tab skipped.")
        return

    logger.info("Writing Size Intelligence tab to Google Sheets...")

    try:
        client = _get_client()
        spreadsheet = client.open_by_key(config.GOOGLE_SHEETS_ID)

        try:
            worksheet = spreadsheet.worksheet(config.SHEETS_SIZE_TAB_NAME)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=config.SHEETS_SIZE_TAB_NAME, rows=2000, cols=len(_HEADERS)
            )

        # Build all cell rows and track formatting metadata
        rows: list[list] = [_HEADERS]
        data_row_meta: list[tuple[int, str]] = []

        for parent in size_products:
            for size in parent["sizes"]:
                row_0 = len(rows)
                rows.append(_size_row(parent, size))
                data_row_meta.append((row_0, size["health_flag"]))

        # Append production brief
        brief_start_row = len(rows)
        rows.extend(_production_brief_rows(size_products))

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

        # Data rows — health flag coloring
        for row_0, flag in data_row_meta:
            color = _FLAG_COLORS.get(flag, _FLAG_COLORS["OK"])
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

        # Production brief header row color: blank + title + blank + column headers = +3
        brief_header_row = brief_start_row + 3
        if brief_header_row < len(rows):
            req.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": brief_header_row, "endRowIndex": brief_header_row + 1,
                        "startColumnIndex": 0, "endColumnIndex": 5,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _COLOR_BRIEF_BG,
                            "textFormat": {"bold": True},
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            })

        # Freeze header row + auto-resize
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

        logger.info(
            "Size Intelligence tab updated. %d parent SKUs, %d size rows written.",
            len(size_products),
            len(data_row_meta),
        )

    except Exception as exc:
        logger.error("Size Intelligence sheet write failed: %s", exc)
        raise
