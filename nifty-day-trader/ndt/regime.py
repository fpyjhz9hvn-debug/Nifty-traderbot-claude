"""Market regime for the NIFTY -- the "when not to trade" gate.

Ported from two skills in github.com/tradermonty/claude-trading-skills:

* `ibd-distribution-day-monitor` -- an IBD-style distribution day is a session
  that closes at least 0.2 % below the previous close on HIGHER volume. It stays
  active for 25 sessions, and is cancelled early once the index has gained 5 %
  from the distribution day's close. The d5 / d15 / d25 cluster counts, plus
  whether the index sits under its 21 EMA or 50 SMA, classify market risk as
  NORMAL, CAUTION, HIGH or SEVERE.
* `exposure-coach` -- uptrend participation (how many names are actually in an
  uptrend) maps to a regime and an exposure ceiling: Broadening, Concentration,
  Transitional or Contraction.

Why it matters here: this app only ever buys. A long-only breakout system takes
its worst losses when the whole market is rolling over and every breakout fails.
This module is the gate that says "not today", and the Risk Manager enforces it.

Everything is measured, nothing is predicted. The regime describes what the
market has already done.
"""
from __future__ import annotations

from . import clock, data, store, universe

INDEX = "^NSEI"

# Distribution-day rules, from the skill.
DD_DROP_PCT = 0.2
DD_WINDOW = 25
DD_INVALIDATE_GAIN_PCT = 5.0


def _ema(values: list, period: int):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for value in values[period:]:
        ema = value * k + ema * (1 - k)
    return ema


def _sma(values: list, period: int):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def distribution_days(bars: list) -> dict:
    """IBD-style distribution days on the index's daily bars."""
    records = []
    for i in range(1, len(bars)):
        prev, today = bars[i - 1], bars[i]
        if not prev["c"] or not today["v"] or not prev["v"]:
            continue
        drop = (prev["c"] - today["c"]) / prev["c"] * 100
        if drop >= DD_DROP_PCT and today["v"] > prev["v"]:
            records.append({"index": i, "date": today["date"], "close": today["c"],
                            "drop_pct": round(drop, 2),
                            "volume": today["v"], "prev_volume": prev["v"]})

    last_index = len(bars) - 1
    active = []
    for rec in records:
        age = last_index - rec["index"]
        if age > DD_WINDOW:
            rec["status"] = "expired"
            continue
        after = bars[rec["index"] + 1:]
        high_since = max((b["h"] for b in after), default=None)
        if high_since and rec["close"] and \
                (high_since - rec["close"]) / rec["close"] * 100 >= DD_INVALIDATE_GAIN_PCT:
            rec["status"] = "invalidated by a 5% gain"
            continue
        rec["status"] = "active"
        rec["age_sessions"] = age
        active.append(rec)

    return {
        "active": active,
        "d5": sum(1 for r in active if r["age_sessions"] <= 5),
        "d15": sum(1 for r in active if r["age_sessions"] <= 15),
        "d25": sum(1 for r in active if r["age_sessions"] <= 25),
        "found": len(records),
    }


def _risk_level(counts: dict, below_ma: bool) -> str:
    d5, d15, d25 = counts["d5"], counts["d15"], counts["d25"]
    if d25 >= 6 or d15 >= 4 or (below_ma and d25 >= 5):
        return "SEVERE"
    if d25 >= 5 or d15 >= 3 or d5 >= 2:
        return "HIGH"
    if d25 >= 3:
        return "CAUTION"
    return "NORMAL"


def participation(group: str = "nifty50") -> dict:
    """How many names are actually in an uptrend, from daily closes."""
    symbols = universe.symbols(group)
    series = data.daily_closes(symbols, rng="6mo")
    above20 = above50 = up5 = counted = 0
    for sym in symbols:
        row = series.get(sym)
        if not row or len(row["closes"]) < 51:
            continue
        closes = row["closes"]
        counted += 1
        ma20, ma50 = _sma(closes, 20), _sma(closes, 50)
        if ma20 and closes[-1] > ma20:
            above20 += 1
        if ma50 and closes[-1] > ma50:
            above50 += 1
        if len(closes) >= 6 and closes[-1] > closes[-6]:
            up5 += 1
    if not counted:
        return {"counted": 0}
    return {
        "counted": counted,
        "above_ma20_pct": round(above20 / counted * 100, 1),
        "above_ma50_pct": round(above50 / counted * 100, 1),
        "up_5d_pct": round(up5 / counted * 100, 1),
        "uptrend_pct": round(above50 / counted * 100, 1),
    }


def _regime_label(uptrend_pct: float, index_above_ma: bool, risk: str):
    """The exposure-coach regime map, on NIFTY numbers."""
    if risk == "SEVERE" or uptrend_pct < 20:
        return "Contraction", (10, 30), "Defensive / cash", "CASH_PRIORITY"
    if not index_above_ma or risk == "HIGH" or uptrend_pct < 25:
        return "Transitional", (40, 60), "Neutral / defensive", "REDUCE_ONLY"
    if uptrend_pct < 40:
        return "Concentration", (60, 80), "Leaders only", "SELECTIVE"
    return "Broadening", (80, 100), "Broad participation", "NEW_ENTRY_ALLOWED"


def assess(group: str = "nifty50", write: bool = True) -> dict:
    """The full regime read: index trend, distribution days, participation."""
    chart = data.chart(INDEX, rng="6mo", interval="1d", ttl=900)
    bars = chart["bars"]
    if len(bars) < 55:
        raise data.DataError("Only %d daily bars for the NIFTY: not enough for a regime read."
                             % len(bars))
    closes = [b["c"] for b in bars]
    last = closes[-1]
    ema21, sma50 = _ema(closes, 21), _sma(closes, 50)
    below_ma = bool((ema21 and last < ema21) or (sma50 and last < sma50))
    above_both = bool(ema21 and sma50 and last > ema21 and last > sma50)

    counts = distribution_days(bars)
    risk = _risk_level(counts, below_ma)
    part = participation(group)
    uptrend = part.get("uptrend_pct", 0.0)
    regime, ceiling, bias, recommendation = _regime_label(uptrend, above_both, risk)

    # The exposure ceiling starts at the regime's midpoint and is adjusted,
    # exactly as the skill's within-regime rules do.
    ceiling_pct = (ceiling[0] + ceiling[1]) / 2
    adjustments = []
    if risk == "SEVERE":
        ceiling_pct, recommendation = min(ceiling_pct, 25), "CASH_PRIORITY"
        adjustments.append("SEVERE distribution risk forces a cash priority.")
    elif risk == "HIGH":
        ceiling_pct -= 15
        adjustments.append("HIGH distribution risk: ceiling cut 15 points.")
    elif risk == "CAUTION":
        ceiling_pct -= 10
        adjustments.append("CAUTION distribution risk: ceiling cut 10 points.")
    if part.get("above_ma20_pct", 0) < 30:
        ceiling_pct -= 10
        adjustments.append("Fewer than 30%% of names are above their 20-day average "
                           "(%.0f%%): ceiling cut 10 points." % part.get("above_ma20_pct", 0))
    ceiling_pct = max(0.0, min(100.0, round(ceiling_pct)))
    if ceiling_pct <= 25 and recommendation != "CASH_PRIORITY":
        recommendation = "REDUCE_ONLY"

    allows_entries = recommendation in ("NEW_ENTRY_ALLOWED", "SELECTIVE")
    out = {
        "as_of": clock.fmt_dt(clock.now_ist()),
        "index": "NIFTY 50",
        "last_close": last,
        "last_session": bars[-1]["date"],
        "ema21": round(ema21, 2) if ema21 else None,
        "sma50": round(sma50, 2) if sma50 else None,
        "above_ema21": bool(ema21 and last > ema21),
        "above_sma50": bool(sma50 and last > sma50),
        "distribution": {
            "d5": counts["d5"], "d15": counts["d15"], "d25": counts["d25"],
            "active": [{"date": r["date"], "drop_pct": r["drop_pct"],
                        "age_sessions": r["age_sessions"]} for r in counts["active"]],
        },
        "risk_level": risk,
        "participation": part,
        "regime": regime,
        "bias": bias,
        "exposure_ceiling_pct": ceiling_pct,
        "recommendation": recommendation,
        "allows_new_entries": allows_entries,
        "adjustments": adjustments,
        "rationale": _rationale(regime, risk, uptrend, part, counts, recommendation),
        "source": ("Distribution days and the 21 EMA / 50 SMA from the NIFTY 50 index's own "
                   "daily bars; participation from the %d names in the %s list."
                   % (part.get("counted", 0), group)),
        "note": ("This describes what the market has already done. It is not a forecast. The "
                 "point of the gate is that a long-only breakout system loses worst when every "
                 "breakout is failing, and that is visible before you take the trade."),
    }
    if write:
        store.write("regime.json", out)
    return out


def _rationale(regime, risk, uptrend, part, counts, recommendation) -> str:
    bits = ["%s regime: %.0f%% of the list is above its 50-day average and %.0f%% above its "
            "20-day." % (regime, uptrend, part.get("above_ma20_pct", 0))]
    if counts["d25"]:
        bits.append("%d distribution day(s) in the last 25 sessions (%d in the last 5), so "
                    "market risk reads %s." % (counts["d25"], counts["d5"], risk))
    else:
        bits.append("No active distribution days, so market risk reads %s." % risk)
    bits.append({
        "NEW_ENTRY_ALLOWED": "New long entries are allowed.",
        "SELECTIVE": "Take only the strongest setups; this is not a broad tape.",
        "REDUCE_ONLY": "No new long entries: manage what is open and let it go flat.",
        "CASH_PRIORITY": "Protect capital. No new long entries at all.",
    }[recommendation])
    return " ".join(bits)


def cached(max_age_minutes: int = 45) -> dict | None:
    """The last regime read, if it is recent enough to still mean something."""
    saved = store.read("regime.json")
    if not saved:
        return None
    try:
        age = (clock.now_ist() - clock.parse_ist(saved["as_of"])).total_seconds() / 60
    except (ValueError, KeyError):
        return None
    saved["age_minutes"] = round(age, 1)
    saved["stale"] = age > max_age_minutes
    return saved
