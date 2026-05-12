"""
Central configuration: loads .env, exposes typed constants, configures logging.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_file_handler = RotatingFileHandler(
    LOGS_DIR / "reorder.log",
    maxBytes=5 * 1024 * 1024,  # 5 MB
    backupCount=7,
    encoding="utf-8",
)
_file_handler.setFormatter(_fmt)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])

logger = logging.getLogger("reorder_engine")


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.error("Missing required environment variable: %s", name)
        raise EnvironmentError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default)


# ── WooCommerce ───────────────────────────────────────────────────────────────
WC_URL = _require("WC_URL").rstrip("/")
WC_CONSUMER_KEY = _require("WC_CONSUMER_KEY")
WC_CONSUMER_SECRET = _require("WC_CONSUMER_SECRET")

# ── Nuport ────────────────────────────────────────────────────────────────────
NUPORT_API_KEY = _require("NUPORT_API_KEY")
NUPORT_BASE_URL = _require("NUPORT_BASE_URL").rstrip("/")

# ── Zoho Books ────────────────────────────────────────────────────────────────
ZOHO_CLIENT_ID = _require("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = _require("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = _require("ZOHO_REFRESH_TOKEN")
ZOHO_ORG_ID = _require("ZOHO_ORG_ID")

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
CLAUDE_MODEL = "claude-sonnet-4-20250514"

# ── Google Sheets ─────────────────────────────────────────────────────────────
GOOGLE_SHEETS_ID = _require("GOOGLE_SHEETS_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = _require("GOOGLE_SERVICE_ACCOUNT_JSON")
SHEETS_TAB_NAME = "Reorder Queue"

# ── Gmail ─────────────────────────────────────────────────────────────────────
GMAIL_SENDER = _require("GMAIL_SENDER")
REORDER_EMAIL = _require("REORDER_EMAIL")

# ── WhatsApp ──────────────────────────────────────────────────────────────────
WHATSAPP_NUMBER = _require("WHATSAPP_NUMBER")
TWILIO_ACCOUNT_SID = _require("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _require("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = _require("TWILIO_WHATSAPP_FROM")

# ── Business constants ────────────────────────────────────────────────────────
TIMEZONE = "Asia/Dhaka"
CURRENCY = "BDT"

LOOKBACK_DAYS = 30            # how far back to pull orders / shipments
VELOCITY_SHORT_DAYS = 7       # short window for trend detection
VELOCITY_LONG_DAYS = 14       # primary velocity window

REORDER_TRIGGER_DAYS = 20     # alert when days_remaining <= this
REORDER_BUFFER_MULTIPLIER = 1.3
REORDER_HORIZON_DAYS = 30     # how many days of supply to order

URGENCY_CRITICAL_THRESHOLD = 7   # days remaining <= this → CRITICAL
URGENCY_WARNING_THRESHOLD = 20   # days remaining <= this → WARNING

MAX_API_RETRIES = 3
RETRY_BACKOFF_BASE = 2        # seconds; actual delay = base ** attempt

# ── Order status filters ───────────────────────────────────────────────────────
# WooCommerce: pull these 4, ignore cancelled + refunded
WC_ACTIVE_STATUSES = ["pending", "processing", "on-hold", "completed"]

# Nuport: pull these 4, ignore flagged (returns) + cancelled (no-answer/rejected)
NUPORT_ACTIVE_STATUSES = ["pending", "on-hold", "in-transit", "delivered"]
# on-hold in Nuport = pre-orders; tracked separately for demand forecasting
NUPORT_PREORDER_STATUS = "on-hold"

# ── Cancellation rate (global, applied to all SKUs) ───────────────────────────
# ~15% of orders cancel before delivery (Nuport: cancelled = no-answer / rejected)
CANCEL_RATE = 0.150

# ── Per-SKU return rate thresholds ────────────────────────────────────────────
# Return rates are now computed per SKU from Nuport flagged data.
# RETURN_RATE_FALLBACK is used for new products with no flagged history.
RETURN_RATE_FALLBACK = 0.35    # conservative default for new/data-less SKUs

# Early warning: flag SKU if 7-day rate exceeds 30-day rate by this many points
RETURN_EARLY_WARNING_THRESHOLD = 0.10   # 10 percentage points

# Cell-level color rules in Google Sheets
RETURN_HIGH_RISK_THRESHOLD = 0.40   # >40% → orange cell "High Return Risk"
RETURN_HOLD_THRESHOLD      = 0.50   # >50% → red cell + "⛔ Hold — Human Review Required"

# ── Size ratio optimization ────────────────────────────────────────────────────
# Recognized size tokens used to detect size variant sub-SKUs.
# A SKU ending in one of these (after the last dash) is treated as a size variant.
# e.g. TS-042-XL → parent=TS-042, size=XL
KNOWN_SIZES = {"XS", "S", "M", "L", "XL", "XXL", "2XL", "3XL", "4XL", "XXXL", "XXXXL"}

# Display order for sizes in reports and sheets
SIZE_DISPLAY_ORDER = ["XS", "S", "M", "L", "XL", "XXL", "2XL", "3XL", "4XL", "XXXL", "XXXXL"]

# Size health thresholds (based on 14-day sell-through)
SIZE_VELOCITY_WINDOW = 14            # days used for size velocity + sell-through
SIZE_FAST_MOVER_SELL_THROUGH = 0.80  # >80% sell-through in 14d → 🔥 Fast Mover
SIZE_SLOW_MOVER_SELL_THROUGH = 0.20  # <20% sell-through in 14d → 🧊 Slow Mover
SIZE_OVERSTOCK_DAYS = 60             # stock > 60d of velocity → ⚠️ Overstock Risk

SHEETS_SIZE_TAB_NAME = "Size Intelligence"
SHEETS_KILL_CHAIN_TAB_NAME = "Kill Chain"
