"""A bank of long-only intraday setups, and one engine that trades them all.

Every setup is a function of one session's 5-minute bars that returns the bar to
enter on and where the stop goes, or nothing. The engine then walks the rest of
the session identically for all of them -- stop checked before target, a bar
touching both counts as the stop, flat by the configured time -- so the only
thing that differs between results is the setup itself.

Why these seven. The first backtest of the opening-range breakout came back at
109 stops against 38 targets. A market that hits the stop three times for every
target on a breakout rule is telling you it mean-reverts intraday rather than
trends, so most of this bank tests the opposite idea: buy weakness that stops
going down, not strength that has already moved. Two trend setups are kept in
to check that reading rather than assume it.

All of them are long-only, because the app is. None of them was tuned: each one
is the plainest version of its idea.
"""
from __future__ import annotations

import math

from . import clock, scanner

# ---------------------------------------------------------------- indicators


def _vwap_series(bars: list) -> list:
    """Session VWAP, bar by bar, from the typical price."""
    out, cum_pv, cum_v = [], 0.0, 0.0
    for bar in bars:
        typical = (bar["h"] + bar["l"] + bar["c"]) / 3
        volume = bar["v"] or 0
        cum_pv += typical * volume
        cum_v += volume
        out.append(cum_pv / cum_v if cum_v else bar["c"])
    return out


def _ema_series(values: list, period: int) -> list:
    k = 2 / (period + 1)
    out, ema = [], None
    for i, value in enumerate(values):
        if i < period - 1:
            out.append(None)
            continue
        if ema is None:
            ema = sum(values[:period]) / period
        else:
            ema = value * k + ema * (1 - k)
        out.append(ema)
    return out


def _lowest(bars: list, start: int, end: int) -> float:
    return min(b["l"] for b in bars[start:end + 1])


# -------------------------------------------------------------------- setups
# Each returns (entry_index, stop_price, label) or None.
# `ctx` carries the opening range, the previous close and the entry window.


def orb_breakout(bars, ctx):
    """The baseline. First bar to CLOSE above the opening-range high."""
    rng = ctx["range"]
    if not rng:
        return None
    for i in range(ctx["first_slot"], ctx["last_slot"] + 1):
        if i < rng["bars"]:
            continue
        if bars[i]["c"] > rng["high"]:
            return i, rng["low"], "closed above the opening-range high"
    return None


def orb_breakout_volume(bars, ctx):
    """The same, but the trigger bar must carry 1.5x the range's volume."""
    rng = ctx["range"]
    if not rng or not rng["avg_volume"]:
        return None
    for i in range(ctx["first_slot"], ctx["last_slot"] + 1):
        if i < rng["bars"]:
            continue
        if bars[i]["c"] > rng["high"] and bars[i]["v"] >= 1.5 * rng["avg_volume"]:
            return i, rng["low"], "closed above the opening range on 1.5x volume"
    return None


def or_low_reclaim(bars, ctx):
    """Failed breakdown: it loses the opening-range low, then closes back above it.

    The mirror image of the breakout, and the first thing to try when breakouts
    are failing: the move that traps sellers rather than the one that chases.
    """
    rng = ctx["range"]
    if not rng:
        return None
    broke = False
    for i in range(rng["bars"], ctx["last_slot"] + 1):
        if bars[i]["c"] < rng["low"]:
            broke = True
            continue
        if broke and i >= ctx["first_slot"] and bars[i]["c"] > rng["low"]:
            return i, _lowest(bars, rng["bars"], i), "reclaimed the opening-range low"
    return None


def vwap_reclaim(bars, ctx):
    """Price falls at least 0.5% under VWAP, then a bar closes back above it."""
    vwap = ctx["vwap"]
    stretched = False
    for i in range(ctx["first_slot"], ctx["last_slot"] + 1):
        if not vwap[i]:
            continue
        if bars[i]["c"] < vwap[i] * 0.995:
            stretched = True
            continue
        if stretched and bars[i]["c"] > vwap[i]:
            return i, _lowest(bars, max(0, i - 6), i), "reclaimed VWAP after stretching below it"
    return None


def gap_down_reclaim(bars, ctx):
    """Opens at least 0.4% below the previous close, then takes out the opening range high."""
    rng, prev = ctx["range"], ctx["prev_close"]
    if not rng or not prev:
        return None
    if bars[0]["o"] >= prev * 0.996:
        return None
    for i in range(ctx["first_slot"], ctx["last_slot"] + 1):
        if i < rng["bars"]:
            continue
        if bars[i]["c"] > rng["high"]:
            return i, rng["low"], "gapped down, then cleared the opening range"
    return None


def pullback_to_ema(bars, ctx):
    """A trend setup: in a session holding above VWAP, buy the first touch of the 20-bar EMA."""
    vwap, ema = ctx["vwap"], ctx["ema20"]
    for i in range(max(ctx["first_slot"], 20), ctx["last_slot"] + 1):
        if not ema[i] or not vwap[i]:
            continue
        if bars[i]["c"] <= vwap[i]:
            continue
        if bars[i]["l"] <= ema[i] and bars[i]["c"] > ema[i]:
            return i, _lowest(bars, max(0, i - 4), i), "held above VWAP and bounced off its 20-bar EMA"
    return None


def first_hour_low_reclaim(bars, ctx):
    """After the first hour, the first bar to close back above the first hour's low."""
    hour = 12  # 12 five-minute bars
    if len(bars) <= hour + 2:
        return None
    low = min(b["l"] for b in bars[:hour])
    broke = False
    for i in range(max(hour, ctx["first_slot"]), ctx["last_slot"] + 1):
        if bars[i]["c"] < low:
            broke = True
            continue
        if broke and bars[i]["c"] > low:
            return i, _lowest(bars, hour, i), "reclaimed the first hour's low"
    return None


BANK = [
    {"key": "orb_breakout", "name": "Opening-range breakout", "family": "trend",
     "fn": orb_breakout,
     "idea": "Buy the first 5-minute close above the first 15 minutes' high. The app's shipped setup."},
    {"key": "orb_breakout_volume", "name": "Opening-range breakout, 1.5x volume", "family": "trend",
     "fn": orb_breakout_volume,
     "idea": "The same breakout, but only when the trigger bar carries real volume."},
    {"key": "or_low_reclaim", "name": "Opening-range low reclaim", "family": "mean reversion",
     "fn": or_low_reclaim,
     "idea": "It breaks the opening-range low, then closes back above it: a failed breakdown."},
    {"key": "vwap_reclaim", "name": "VWAP reclaim", "family": "mean reversion",
     "fn": vwap_reclaim,
     "idea": "Price stretches 0.5% under VWAP, then closes back above it."},
    {"key": "gap_down_reclaim", "name": "Gap-down reclaim", "family": "mean reversion",
     "fn": gap_down_reclaim,
     "idea": "Opens 0.4% or more below yesterday's close, then clears the opening range."},
    {"key": "pullback_to_ema", "name": "Pullback to the 20-bar EMA", "family": "trend",
     "fn": pullback_to_ema,
     "idea": "In a session holding above VWAP, buy the first bounce off the 20-bar EMA."},
    {"key": "first_hour_low_reclaim", "name": "First-hour low reclaim", "family": "mean reversion",
     "fn": first_hour_low_reclaim,
     "idea": "After the first hour, buy the first close back above that hour's low."},
]

BY_KEY = {s["key"]: s for s in BANK}


# -------------------------------------------------------------------- engine

def context(bars: list, or_minutes: int, prev_close, first_slot: int, last_slot: int) -> dict:
    return {
        "range": scanner.opening_range(bars, or_minutes),
        "vwap": _vwap_series(bars),
        "ema20": _ema_series([b["c"] for b in bars], 20),
        "prev_close": prev_close,
        "first_slot": first_slot,
        "last_slot": min(last_slot, len(bars) - 1),
    }


def simulate(bars: list, setup: dict, ctx: dict, target_r: float):
    """Run one setup on one session. Stop before target, flat at the cut-off."""
    found = setup["fn"](bars, ctx)
    if not found:
        return None
    entry_index, raw_stop, why = found
    if entry_index >= ctx["last_slot"]:
        return None
    entry = bars[entry_index]["c"]
    stop = math.floor(float(raw_stop) * 100) / 100
    risk = entry - stop
    if risk <= 0 or risk / entry < 0.0005 or risk / entry > 0.05:
        return None  # no stop, a stop too tight for the spread, or an absurd one
    target = entry + target_r * risk
    outcome, exit_price, exit_index = "timed out", bars[ctx["last_slot"]]["c"], ctx["last_slot"]
    for i in range(entry_index + 1, ctx["last_slot"] + 1):
        bar = bars[i]
        if bar["l"] <= stop:
            outcome, exit_price, exit_index = "stop", stop, i
            break
        if bar["h"] >= target:
            outcome, exit_price, exit_index = "target", target, i
            break
    return {
        "setup": setup["key"],
        "entry_index": entry_index,
        "entry_time": bars[entry_index]["end"],
        "entry": round(entry, 2),
        "stop": stop,
        "target": round(target, 2),
        "risk_per_share": round(risk, 2),
        "exit": round(exit_price, 2),
        "exit_time": bars[exit_index]["end"],
        "outcome": outcome,
        "why": why,
        "r": round((exit_price - entry) / risk, 3),
    }


def window_slots(cfg: dict):
    """The first and last bar index an entry may be taken on."""
    open_min = clock.SESSION_OPEN.hour * 60 + clock.SESSION_OPEN.minute
    first = clock.parse_hhmm(cfg.get("no_entries_before", "09:30"))
    flat = clock.parse_hhmm(cfg.get("flat_by", "15:20"))
    first_slot = max(0, ((first.hour * 60 + first.minute) - open_min) // clock.BAR_MINUTES - 1)
    last_slot = min(clock.BARS_PER_SESSION - 1,
                    ((flat.hour * 60 + flat.minute) - open_min) // clock.BAR_MINUTES - 1)
    return first_slot, last_slot
