"""
Write the reorder queue to a Google Sheet.

Sheet: SHEETS_TAB_NAME ("Reorder Queue")
- Overwrites all data every run (not append)
- Row color by urgency tier (red/yellow/green)
- Per-cell color on Return Rate % column:
    >50% → red cell + "⛔ Hold — Human Review Required"
    >40% → orange cell + "⚠️ High Return Risk"
- Timestamps every update
- New columns vs original: Gross Velocity | Return Rate % | Net Velocity
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
    "30d Orders",
    "Current Stock",
    "Gross Velocity (14d)",
    "Return Rate %",
    "Net Velocity (14d)",
    "Days Remaining",
    "Urgency",
    "Reorder Qty",
    "Est. Cost (BDT)",
    "Return Warning",
    "Claude Recommendation",
    "Last Updated",
]

# Column index (0-based) of the "Return Rate %" column — used for per-cell coloring
_RETURN_RATE_COL_IDX = _HEADERS.index("Return Rate %")

# gspread color dicts (RGB 0–1 scale)
_COLOR_ROW_RED    = {"red": 0.96, "green": 0.80, "blue": 0.80}
_COLOR_ROW_YELLOW = {"red": 1.00, "green": 0.97, "blue": 0.80}
_COLOR_ROW_GREEN  = {"red": 0.85, "green": 0.93, "blue": 0.83}
_COLOR_HEADER     = {"red": 0.23, "green": 0.23, "blue": 0.23}
_COLOR_WHITE      = {"red": 1.00, "green": 1.00, "blue": 1.00}

# Per-cell return rate colors
_COLOR_CELL_ORANGE = {"red": 1.00, "green": 0.60, "blue": 0.20}  # >40%
_COLOR_CELL_RED    = {"red": 0.90, "green": 0.20, "blue": 0.20}  # >50%


def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(
        config.GOOGLE_SERVICE_ACCOUNT_JSON,
        scopes=_SCOPES,
    )
    return gspread.authorize(creds)


def _urgency_row_color(tier: str) -> dict:
    if tier == "CRITICAL":
        return _COLOR_ROW_RED
    if tier == "WARNING":
        return _COLOR_ROW_YELLOW
    return _COLOR_ROW_GREEN


def _return_rate_cell_value(rate: float, hold: bool, high_risk: bool) -> str:
    """Format the return rate cell text — includes a flag label when thresholds are exceeded."""
    pct = f"{rate * 100:.1f}%"
    if hold:
        return f"{pct} ⛔ Hold — Human Review Required"
    if high_risk:
        return f"{pct} ⚠️ High Return Risk"
    return pct


def _return_warning_cell(sku_data: dict) -> str:
    """Build the Return Warning column cell text."""
    if not sku_data.get("return_early_warning"):
        return ""
    rate_30d = sku_data.get("return_rate_30d", 0)
    rate_7d = sku_data.get("return_rate_7d", 0)
    spike = (rate_7d - rate_30d) * 100
    action = sku_data.get("claude_return_action", "")
    text = f"⚠️ SPIKE +{spike:.1f}pp ({rate_30d*100:.0f}% → {rate_7d*100:.0f}%)"
    if action:
        text += f" | {action}"
    return text


def _row_for_sku(sku_data: dict, updated_at: str) -> list:
    return_rate = sku_data.get("return_rate_30d", config.RETURN_RATE_FALLBACK)
    hold = sku_data.get("hold_for_review", False)
    high_risk = sku_data.get("high_return_risk", False)

    recommendation = " | ".join(
        filter(None, [
            sku_data.get("claude_trend_note", ""),
            sku_data.get("claude_action_note", ""),
            sku_data.get("claude_risk_flag", "") if sku_data.get("claude_risk_flag") not in ("NONE", "N/A", "") else "",
        ])
    ) or "—"

    # Kill chain block overrides reorder recommendation
    kc_stage = sku_data.get("kill_chain_stage")
    kc_label = sku_data.get("kill_chain_stage_label", "")
    if sku_data.get("kill_chain_blocked") and kc_stage:
        recommendation = f"⛔ Blocked — Kill Chain Stage {kc_stage} ({kc_label}) | {recommendation}"
    elif hold:
        recommendation = "⛔ HOLD — Human Review Required | " + recommendation

    days = sku_data.get("days_remaining", 9999)
    days_display = "∞" if days >= 9999 else str(days)

    reorder_qty = sku_data.get("claude_recommended_qty") or sku_data.get("reorder_qty", 0)
    purchase_price = sku_data.get("last_purchase_price", 0.0) or 0.0
    estimated_cost = round(reorder_qty * purchase_price, 0) if purchase_price else ""

    return [
        sku_data.get("sku", ""),
        sku_data.get("product_name", ""),
        sku_data.get("total_ordered", 0),
        sku_data.get("current_stock", 0),
        round(sku_data.get("raw_velocity_14d", 0.0), 2),
        _return_rate_cell_value(return_rate, hold, high_risk),
        round(sku_data.get("net_velocity_14d", 0.0), 2),
        days_display,
        sku_data.get("urgency_tier", "HEALTHY"),
        reorder_qty,
        estimated_cost,
        _return_warning_cell(sku_data),
        recommendation,
        updated_at,
    ]


def write_to_sheets(all_skus: list[dict]) -> None:
    """
    Overwrite the Reorder Queue sheet with current SKU data.

    Sort order: best-selling (30d orders) descending — top sellers at the top
    so you can easily scan top 100/200 products. Urgency tier is shown as a
    column but is NOT the primary sort — sales volume is.
    Row color: urgency tier (red=critical, yellow=warning, green=healthy).
    Return Rate % cell: orange (>40%) or red (>50%) overrides row color.
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

        # Primary sort: 30d orders descending (best sellers first)
        # Secondary: days remaining ascending (most urgent within same sales volume)
        sorted_skus = sorted(
            all_skus,
            key=lambda s: (
                -s.get("total_ordered", 0),
                s.get("days_remaining", 9999),
            ),
        )

        rows = [_HEADERS]
        rows.extend(_row_for_sku(s, updated_at) for s in sorted_skus)

        worksheet.clear()
        worksheet.update("A1", rows)

        # ── Formatting batch ──────────────────────────────────────────────────
        total_cols = len(_HEADERS)
        sheet_id = worksheet.id
        requests_body = []

        # Header row styling
        requests_body.append({
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

        # Data rows: urgency row color, then return rate cell overrides
        for row_idx, sku_data in enumerate(sorted_skus, start=1):
            row_color = _urgency_row_color(sku_data.get("urgency_tier", "HEALTHY"))

            # Full row background by urgency
            requests_body.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": 0, "endColumnIndex": total_cols,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": row_color}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

            # Per-cell override on Return Rate % column
            return_rate = sku_data.get("return_rate_30d", config.RETURN_RATE_FALLBACK)
            if return_rate > config.RETURN_HOLD_THRESHOLD:
                cell_color = _COLOR_CELL_RED
            elif return_rate > config.RETURN_HIGH_RISK_THRESHOLD:
                cell_color = _COLOR_CELL_ORANGE
            else:
                cell_color = None

            if cell_color:
                requests_body.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                            "startColumnIndex": _RETURN_RATE_COL_IDX,
                            "endColumnIndex": _RETURN_RATE_COL_IDX + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": cell_color,
                                "textFormat": {"bold": True},
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat)",
                    }
                })

        # Auto-resize columns
        requests_body.append({
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0, "endIndex": total_cols,
                }
            }
        })

        # Freeze header row
        requests_body.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        })

        spreadsheet.batch_update({"requests": requests_body})

        logger.info(
            "Google Sheets updated. %d SKU rows written to '%s'.",
            len(sorted_skus), config.SHEETS_TAB_NAME,
        )

    except Exception as exc:
        logger.error("Google Sheets write failed: %s", exc)
        raise
