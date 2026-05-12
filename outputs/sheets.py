"""
Write the reorder queue to a Google Sheet.

Sheet: SHEETS_TAB_NAME ("Reorder Queue")
- Overwrites all data every run (not append)
- Color-codes rows by urgency tier: red / yellow / green
- Timestamps every update in cell A1 area header
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
    "SKU",
    "Product Name",
    "Current Stock",
    "Daily Velocity (14d)",
    "Days Remaining",
    "True Demand",
    "Urgency",
    "Reorder Qty",
    "Est. Cost (BDT)",
    "Claude Recommendation",
    "Last Updated",
]

# gspread color dicts (RGB 0–1 scale)
_COLOR_RED = {"red": 0.96, "green": 0.80, "blue": 0.80}
_COLOR_YELLOW = {"red": 1.0, "green": 0.97, "blue": 0.80}
_COLOR_GREEN = {"red": 0.85, "green": 0.93, "blue": 0.83}
_COLOR_HEADER = {"red": 0.23, "green": 0.23, "blue": 0.23}
_COLOR_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}


def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(
        config.GOOGLE_SERVICE_ACCOUNT_JSON,
        scopes=_SCOPES,
    )
    return gspread.authorize(creds)


def _urgency_color(tier: str) -> dict:
    if tier == "CRITICAL":
        return _COLOR_RED
    if tier == "WARNING":
        return _COLOR_YELLOW
    return _COLOR_GREEN


def _row_for_sku(sku_data: dict, updated_at: str) -> list:
    recommendation = " | ".join(
        filter(
            None,
            [
                sku_data.get("claude_trend_note", ""),
                sku_data.get("claude_action_note", ""),
                sku_data.get("claude_risk_flag", "") if sku_data.get("claude_risk_flag") not in ("NONE", "N/A", "") else "",
            ],
        )
    ) or "—"

    days = sku_data.get("days_remaining", 9999)
    days_display = "∞" if days >= 9999 else str(days)

    return [
        sku_data.get("sku", ""),
        sku_data.get("product_name", ""),
        sku_data.get("current_stock", 0),
        round(sku_data.get("daily_velocity_14d", 0.0), 2),
        days_display,
        sku_data.get("true_demand", 0),
        sku_data.get("urgency_tier", "HEALTHY"),
        sku_data.get("claude_recommended_qty") or sku_data.get("reorder_qty", 0),
        sku_data.get("estimated_cost", 0.0),
        recommendation,
        updated_at,
    ]


def write_to_sheets(all_skus: list[dict]) -> None:
    """
    Overwrite the Reorder Queue sheet with current SKU data.

    Sorts: CRITICAL first, then WARNING, then HEALTHY.
    Each row is color-coded.
    """
    logger.info("Writing to Google Sheets...")

    try:
        client = _get_client()
        spreadsheet = client.open_by_key(config.GOOGLE_SHEETS_ID)

        try:
            worksheet = spreadsheet.worksheet(config.SHEETS_TAB_NAME)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=config.SHEETS_TAB_NAME, rows=1000, cols=len(_HEADERS)
            )

        updated_at = datetime.now().strftime("%Y-%m-%d %H:%M BST")

        # Sort order: CRITICAL → WARNING → HEALTHY, then by days_remaining asc
        _tier_order = {"CRITICAL": 0, "WARNING": 1, "HEALTHY": 2}
        sorted_skus = sorted(
            all_skus,
            key=lambda s: (
                _tier_order.get(s.get("urgency_tier", "HEALTHY"), 2),
                s.get("days_remaining", 9999),
            ),
        )

        rows = [_HEADERS]
        rows.extend(_row_for_sku(s, updated_at) for s in sorted_skus)

        # Overwrite everything
        worksheet.clear()
        worksheet.update("A1", rows)

        # ── Formatting ────────────────────────────────────────────────────────
        total_rows = len(rows)
        total_cols = len(_HEADERS)
        sheet_id = worksheet.id

        requests_body = []

        # Header row: dark background + bold white text
        requests_body.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": total_cols,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _COLOR_HEADER,
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": _COLOR_WHITE,
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            }
        )

        # Data rows: color by urgency
        for row_idx, sku_data in enumerate(sorted_skus, start=1):
            color = _urgency_color(sku_data.get("urgency_tier", "HEALTHY"))
            requests_body.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_idx,
                            "endRowIndex": row_idx + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": total_cols,
                        },
                        "cell": {
                            "userEnteredFormat": {"backgroundColor": color}
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }
            )

        # Auto-resize columns
        requests_body.append(
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": total_cols,
                    }
                }
            }
        )

        # Freeze header row
        requests_body.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            }
        )

        spreadsheet.batch_update({"requests": requests_body})

        logger.info(
            "Google Sheets updated. %d SKU rows written to '%s'.",
            len(sorted_skus), config.SHEETS_TAB_NAME,
        )

    except Exception as exc:
        logger.error("Google Sheets write failed: %s", exc)
        raise
