"""
Dead Stock Kill Chain Command Center.

Scores each SKU on 5 signals (max 100 pts), assigns a Kill Chain stage,
computes financial impact, and flags reorder suppression.

Kill Chain Stages:
  ⚪ WATCH      (50–69)  — monitor only, no action
  🟡 MARKDOWN   (70–84)  — apply discount to clear in ~14 days
  🟠 BUNDLE     (85–92)  — pair with a fast-moving SKU
  🔴 LIQUIDATE  (93–100) — wholesale clearance, capital trapped

Stage 2+ (Markdown / Bundle / Liquidate) SKUs are blocked from Reorder Queue.
Stock age is persisted in logs/stock_age.json (first-seen date per SKU).
"""

import json
from datetime import datetime, timezone

import config
from config import logger

_STOCK_AGE_PATH = config.BASE_DIR / "logs" / "stock_age.json"

# ── Stage constants ───────────────────────────────────────────────────────────
WATCH = "WATCH"
MARKDOWN = "MARKDOWN"
BUNDLE = "BUNDLE"
LIQUIDATE = "LIQUIDATE"

STAGE_LABEL = {
    WATCH:     "⚪ Watch — recheck in 7 days",
    MARKDOWN:  "🟡 Markdown Recommended",
    BUNDLE:    "🟠 Bundle Required",
    LIQUIDATE: "🔴 Liquidate — Capital Trap",
}

# Stage 2+ = Markdown/Bundle/Liquidate → suppressed from Reorder Queue
BLOCKED_STAGES = {MARKDOWN, BUNDLE, LIQUIDATE}

# Assumed gross margin multiplier (cost → selling price) for recovery estimates
_MARKUP = 2.5


# ── Stock age history ─────────────────────────────────────────────────────────

def _load_stock_age() -> dict[str, str]:
    if not _STOCK_AGE_PATH.exists():
        return {}
    try:
        with open(_STOCK_AGE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load stock age history: %s", exc)
        return {}


def _save_stock_age(data: dict[str, str]) -> None:
    try:
        _STOCK_AGE_PATH.parent.mkdir(exist_ok=True)
        with open(_STOCK_AGE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        logger.warning("Could not save stock age history: %s", exc)


# ── Signal scoring ────────────────────────────────────────────────────────────

def _score_days_since_last_sale(days: int) -> int:
    if days <= 7:
        return 0
    if days <= 14:
        return 10
    if days <= 21:
        return 18
    return 25


def _score_sell_through(pct: float) -> int:
    if pct > 0.50:
        return 0
    if pct > 0.30:
        return 8
    if pct > 0.15:
        return 18
    return 25


def _score_stock_age(age_days: int) -> int:
    if age_days < 30:
        return 0
    if age_days < 45:
        return 8
    if age_days < 60:
        return 15
    return 20


def _score_velocity_trend(vel_7d: float, vel_14d: float) -> int:
    if vel_14d <= 0:
        return 15  # zero baseline = near zero
    ratio = vel_7d / vel_14d
    if ratio >= 1.1:
        return 0   # Accelerating
    if ratio >= 0.9:
        return 5   # Flat
    if vel_7d > 0.01:
        return 10  # Declining
    return 15      # Near zero


def _score_return_rate(rate: float) -> int:
    if rate < 0.25:
        return 0
    if rate < 0.40:
        return 5
    if rate < 0.50:
        return 10
    return 15


# ── Stage + financial helpers ─────────────────────────────────────────────────

def _kill_chain_stage(score: int) -> str | None:
    if score >= 93:
        return LIQUIDATE
    if score >= 85:
        return BUNDLE
    if score >= 70:
        return MARKDOWN
    if score >= 50:
        return WATCH
    return None  # Healthy


def _suggested_discount(days_remaining: float) -> float:
    """
    Discount % to clear stock in ~14 days.
    Formula: (days_remaining - 30) × 2%  |  clamped [10%, 40%]
    """
    if days_remaining >= 9999:
        return 0.40
    raw = (days_remaining - 30) * 0.02
    return round(max(0.10, min(0.40, raw)), 2)


def _days_since_last_sale(sku_data: dict) -> int:
    """Compute days since the most recent WooCommerce order date."""
    order_dates = sku_data.get("wc_order_dates") or []
    if not order_dates:
        return config.LOOKBACK_DAYS

    now = datetime.now(timezone.utc)
    most_recent: datetime | None = None

    for d in order_dates:
        if not isinstance(d, datetime):
            try:
                d = datetime.fromisoformat(str(d).replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if most_recent is None or d > most_recent:
            most_recent = d

    if most_recent is None:
        return config.LOOKBACK_DAYS

    return max((now - most_recent).days, 0)


# ── Core engine ───────────────────────────────────────────────────────────────

def compute_dead_stock(enriched_skus: list[dict]) -> list[dict]:
    """
    Score every enriched SKU for dead stock risk.

    Modifies each SKU dict in-place, adding:
      dead_stock_score, kill_chain_stage, kill_chain_stage_label,
      kill_chain_blocked, days_since_last_sale, sell_through_30d_pct,
      stock_age_days, suggested_discount_pct,
      capital_locked, estimated_recovery, write_off_risk

    Returns: list of SKU dicts with score >= 50 (Watch or worse), score-desc.
    """
    age_history = _load_stock_age()
    new_age_history: dict[str, str] = dict(age_history)
    today_str = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now()

    dead_stock: list[dict] = []

    for sku_data in enriched_skus:
        sku = sku_data["sku"]
        current_stock = sku_data.get("current_stock", 0) or 0

        # Zero stock means nothing is stuck — skip dead stock scoring
        if current_stock <= 0:
            _stamp_defaults(sku_data)
            continue

        # ── Signal 1: Days since last sale (25pts) ────────────────────────────
        days_last_sale = _days_since_last_sale(sku_data)
        s1 = _score_days_since_last_sale(days_last_sale)

        # ── Signal 2: Sell-through rate last 30d (25pts) ──────────────────────
        # Extrapolate from 14d daily rate over 30 days
        raw_vel = sku_data.get("raw_velocity_14d", 0.0) or 0.0
        units_sold_30d = raw_vel * 30
        denom = current_stock + units_sold_30d
        sell_through_30d = units_sold_30d / denom if denom > 0 else 0.0
        s2 = _score_sell_through(sell_through_30d)

        # ── Signal 3: Stock age — days since first unit received (20pts) ──────
        if sku not in age_history:
            new_age_history[sku] = today_str
            age_days = 0
        else:
            try:
                first_seen = datetime.strptime(age_history[sku], "%Y-%m-%d")
                age_days = max((now - first_seen).days, 0)
            except ValueError:
                age_days = 0
                new_age_history[sku] = today_str
        s3 = _score_stock_age(age_days)

        # ── Signal 4: Velocity trend — 7d vs 14d (15pts) ─────────────────────
        vel_7d = sku_data.get("net_velocity_7d") or sku_data.get("daily_velocity_7d", 0.0) or 0.0
        vel_14d = sku_data.get("net_velocity_14d") or sku_data.get("daily_velocity_14d", 0.0) or 0.0
        s4 = _score_velocity_trend(vel_7d, vel_14d)

        # ── Signal 5: Return rate (15pts) ─────────────────────────────────────
        return_rate = sku_data.get("return_rate_30d", config.RETURN_RATE_FALLBACK) or 0.0
        s5 = _score_return_rate(return_rate)

        score = s1 + s2 + s3 + s4 + s5
        stage = _kill_chain_stage(score)

        # ── Financial impact ──────────────────────────────────────────────────
        cost_price = sku_data.get("last_purchase_price", 0.0) or 0.0
        capital_locked = round(current_stock * cost_price, 2)

        days_remaining = sku_data.get("days_remaining", 9999) or 9999
        discount_pct = _suggested_discount(days_remaining)

        if stage == MARKDOWN:
            est_recovery = round(current_stock * cost_price * _MARKUP * (1.0 - discount_pct), 2)
        elif stage == BUNDLE:
            est_recovery = round(current_stock * cost_price * 2.0, 2)
        elif stage == LIQUIDATE:
            est_recovery = round(current_stock * cost_price * 0.60, 2)
        elif stage == WATCH:
            est_recovery = round(current_stock * cost_price * _MARKUP * 0.90, 2)
        else:
            est_recovery = round(current_stock * cost_price * _MARKUP, 2)

        write_off_risk = round(max(capital_locked - est_recovery, 0.0), 2)

        # ── Write back to SKU dict ────────────────────────────────────────────
        sku_data["dead_stock_score"]       = score
        sku_data["kill_chain_stage"]       = stage
        sku_data["kill_chain_stage_label"] = STAGE_LABEL.get(stage, "") if stage else ""
        sku_data["kill_chain_blocked"]     = stage in BLOCKED_STAGES
        sku_data["days_since_last_sale"]   = days_last_sale
        sku_data["sell_through_30d_pct"]   = round(sell_through_30d * 100, 1)
        sku_data["stock_age_days"]         = age_days
        sku_data["suggested_discount_pct"] = int(discount_pct * 100)
        sku_data["capital_locked"]         = capital_locked
        sku_data["estimated_recovery"]     = est_recovery
        sku_data["write_off_risk"]         = write_off_risk

        # Defaults for Claude fields (filled by merge_kill_chain_analysis later)
        sku_data.setdefault("claude_kill_chain_stage", "")
        sku_data.setdefault("claude_exit_action", "")
        sku_data.setdefault("claude_brand_risk", "")
        sku_data.setdefault("claude_ops_instruction", "")
        sku_data.setdefault("claude_bundle_with", "")

        if stage:
            dead_stock.append(sku_data)

    _save_stock_age(new_age_history)

    # Sort: Liquidate first, then Bundle, Markdown, Watch; within stage by score desc
    _order = {LIQUIDATE: 0, BUNDLE: 1, MARKDOWN: 2, WATCH: 3}
    dead_stock.sort(key=lambda s: (_order.get(s["kill_chain_stage"], 9), -s["dead_stock_score"]))

    liq = sum(1 for s in dead_stock if s["kill_chain_stage"] == LIQUIDATE)
    bun = sum(1 for s in dead_stock if s["kill_chain_stage"] == BUNDLE)
    mkd = sum(1 for s in dead_stock if s["kill_chain_stage"] == MARKDOWN)
    wtc = sum(1 for s in dead_stock if s["kill_chain_stage"] == WATCH)

    logger.info(
        "Dead stock scoring complete: %d SKUs flagged "
        "(🔴 Liquidate: %d | 🟠 Bundle: %d | 🟡 Markdown: %d | ⚪ Watch: %d)",
        len(dead_stock), liq, bun, mkd, wtc,
    )
    return dead_stock


def _stamp_defaults(sku_data: dict) -> None:
    """Apply zero/None dead stock fields to SKUs not flagged as dead stock."""
    sku_data.setdefault("dead_stock_score", 0)
    sku_data.setdefault("kill_chain_stage", None)
    sku_data.setdefault("kill_chain_stage_label", "")
    sku_data.setdefault("kill_chain_blocked", False)
    sku_data.setdefault("days_since_last_sale", 0)
    sku_data.setdefault("sell_through_30d_pct", 0.0)
    sku_data.setdefault("stock_age_days", 0)
    sku_data.setdefault("suggested_discount_pct", 0)
    sku_data.setdefault("capital_locked", 0.0)
    sku_data.setdefault("estimated_recovery", 0.0)
    sku_data.setdefault("write_off_risk", 0.0)
    sku_data.setdefault("claude_kill_chain_stage", "")
    sku_data.setdefault("claude_exit_action", "")
    sku_data.setdefault("claude_brand_risk", "")
    sku_data.setdefault("claude_ops_instruction", "")
    sku_data.setdefault("claude_bundle_with", "")
