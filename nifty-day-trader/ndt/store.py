"""The trader/ folder: config and state, one JSON file each, like the skill pack.

Every skill in the chain reads what the one before it wrote:

    watchlist.json -> entry.json -> plan.json -> timemachine.json
                   -> orders.json -> state/risk.json -> state/broker.json
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from . import clock

ROOT = Path(__file__).resolve().parent.parent
TRADER = ROOT / "trader"
STATE = TRADER / "state"

_lock = threading.RLock()

# Rupees. These are the defaults a fresh install starts with; the UI edits them.
DEFAULT_CONFIG = {
    "currency": "INR",
    "paper": True,
    "broker": "built-in paper broker (simulated, no real money, no broker account)",
    "budget": 500000.0,
    "cash_buffer_pct": 10.0,
    "max_loss_per_trade": 2500.0,
    "max_position_pct": 25.0,
    "max_order_value": 150000.0,
    "min_order_value": 500.0,
    "max_daily_loss": 7500.0,
    "max_trades_per_day": 3,
    "max_orders_per_day": 6,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "15:00",
    "flat_by": "15:20",
    "or_minutes": 15,
    # or-low rather than the skill pack's or-mid: on the 60-day backtest in
    # ndt/backtest.py it improved expectancy in all four target variants, on the
    # full 177-trade sample, and the mechanism is plain -- a wider stop takes
    # fewer noise stop-outs and buys fewer shares, which cuts the charge drag.
    # In-sample evidence from one window; change it back in Settings.
    "default_stop_rule": "or-low",
    "default_target_r": 2.0,
    "universe_group": "nifty50",
    "blocked": [],
    "nse_holidays": [],
    "allow_short": False,
    "allow_options": False,
    # The discipline layer, ported from claude-trading-skills.
    "use_regime_gate": True,
    "max_daily_loss_pct": 2.0,
    "max_weekly_loss_pct": 5.0,
    "max_monthly_loss_pct": 8.0,
    "losing_streak_n": 2,
    "cooldown_minutes": 45,
    "max_portfolio_heat_pct": 6.0,
    "revenge_window_minutes": 30,
    # Estimated NSE intraday equity charges, so a paper P&L is not fiction.
    # Rates change and your broker's will differ: edit them, or switch them off.
    "apply_costs": True,
    "brokerage_pct": 0.03,
    "brokerage_cap": 20.0,
    "stt_sell_pct": 0.025,
    "exchange_txn_pct": 0.00297,
    "sebi_pct": 0.0001,
    "stamp_buy_pct": 0.003,
    "gst_pct": 18.0,
    # Delivery (held overnight) is taxed differently: no brokerage at most
    # discount brokers, but STT on both sides and higher stamp duty.
    "delivery_brokerage_pct": 0.0,
    "stt_delivery_pct": 0.1,
    "stamp_delivery_buy_pct": 0.015,
    # The position sleeve, ported from trader-skill v1.
    # How much of the account the held sleeve may use. The rest stays as cash
    # for the intraday side, which needs room for its own positions.
    "position_sleeve_pct": 50.0,
    "rebalance_band_pct": 5.0,
    # An absolute points band cannot work for a wide sleeve: 51 equal-weight
    # names are 0.9 % each, so none of them ever drifts 5 points and nothing
    # would ever trade. The effective band is the smaller of the points band
    # and this fraction of the name's own target.
    "rebalance_relative_band_pct": 25.0,
    "position_hold_count": 10,
    "momentum_lookback_days": 126,
    "momentum_skip_days": 21,
}


def ensure_dirs():
    TRADER.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)


def path(name: str) -> Path:
    ensure_dirs()
    return TRADER / name


def read(name: str, default=None):
    p = TRADER / name
    with _lock:
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default


def write(name: str, payload) -> dict:
    ensure_dirs()
    p = TRADER / name
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with _lock:
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, p)
    return payload


def delete(name: str):
    p = TRADER / name
    with _lock:
        if p.exists():
            p.unlink()


def config() -> dict:
    cfg = read("config.json")
    if not isinstance(cfg, dict):
        cfg = {}
        write("config.json", DEFAULT_CONFIG)
        cfg = dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def save_config(updates: dict) -> dict:
    cfg = config()
    for key, value in (updates or {}).items():
        if key in DEFAULT_CONFIG:
            cfg[key] = value
    write("config.json", cfg)
    return cfg


def holidays() -> list:
    return config().get("nse_holidays") or []


def today() -> str:
    return clock.now_ist().strftime("%Y-%m-%d")


def rupees(amount) -> str:
    """1234567.5 -> 'Rs 12,34,567.50' (Indian grouping)."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return "-"
    sign = "-" if amount < 0 else ""
    whole, frac = divmod(round(abs(amount), 2), 1)
    whole = int(whole)
    text = str(whole)
    if len(text) > 3:
        head, tail = text[:-3], text[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        text = ",".join(parts + [tail])
    return "%s₹%s.%02d" % (sign, text, round(frac * 100))
