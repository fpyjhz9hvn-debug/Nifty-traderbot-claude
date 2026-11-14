"""Exit Planner (trader-exit, ported to NSE).

The stop is where the setup has failed. The target is where you take what you
came for. Both are decided BEFORE the entry and written down, so nothing has to
be decided in the middle of a fast move.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Prices round to the paisa (2 decimals); the stop rounds AWAY from the entry.
"""
from __future__ import annotations

import math

from . import clock, data, scanner, store, universe

STOP_RULES = {
    "or-mid": "the opening-range midpoint",
    "or-low": "the opening-range low",
    "swing-low": "the low since the break (for a reclaim setup)",
    "price": "your price",
    "pct": "a percent under the entry",
    "atr": "a multiple of the 14-bar ATR under the entry",
}


class PlanRefused(Exception):
    pass


def _floor2(value: float) -> float:
    return math.floor(value * 100) / 100.0


def _round2(value: float) -> float:
    return math.floor(value * 100 + 0.5) / 100.0


def plan(symbol: str | None = None, entry: float | None = None, stop_rule: str | None = None,
         target_rule: str | None = None, or_high: float | None = None,
         or_low: float | None = None, setup: str = "orb", entry_time: str | None = None,
         entry_bar: int | None = None, write: bool = True) -> dict:
    """Build the exit plan. Reads trader/entry.json when nothing is passed."""
    cfg = store.config()
    saved = store.read("entry.json") or {}

    if symbol is None:
        symbol = saved.get("symbol")
        if not symbol:
            raise PlanRefused("No entry yet. Run the Entry Timer first, or name a symbol.")
        if saved.get("status") != "triggered" and entry is None:
            raise PlanRefused("The saved entry has not triggered. Plan ahead by giving an entry price.")
    symbol = universe.normalise(symbol)

    if entry is None:
        entry = saved.get("entry")
    if entry is None:
        raise PlanRefused("No entry price. Give one, or wait for the Entry Timer to trigger.")
    entry = float(entry)

    same = saved.get("symbol") == symbol
    if or_high is None and same:
        or_high = (saved.get("opening_range") or {}).get("high")
    if or_low is None and same:
        or_low = (saved.get("opening_range") or {}).get("low")
    entry_time = entry_time or (saved.get("entry_time") if same else None) or clock.fmt_time(clock.now_ist())
    entry_bar = entry_bar or (saved.get("entry_bar") if same else None)
    setup = setup or saved.get("setup") or "orb"

    stop_rule = stop_rule or cfg.get("default_stop_rule", "or-mid")
    target_rule = target_rule or ("%sR" % cfg.get("default_target_r", 2.0))

    stop, stop_why = _stop_from_rule(symbol, entry, stop_rule, or_high, or_low)
    stop = _floor2(stop)
    if stop >= entry:
        raise PlanRefused("A stop at %.2f is not below the entry %.2f. A long needs the stop under it."
                          % (stop, entry))

    risk = round(entry - stop, 2)
    if risk <= 0:
        raise PlanRefused("The risk works out to zero. Use a wider stop.")
    if risk / entry < 0.0005:
        raise PlanRefused("A %.2f stop is %.3f%% of the price: too tight to survive the spread, "
                          "normal wiggle would stop you out." % (stop, risk / entry * 100))

    target, target_why, target_r = _target_from_rule(entry, risk, target_rule)
    target = _round2(target)
    if target <= entry:
        raise PlanRefused("A target at %.2f is not above the entry %.2f." % (target, entry))

    flat_by = cfg.get("flat_by", "15:20")
    if entry_time and clock.parse_hhmm(entry_time) >= clock.parse_hhmm(flat_by):
        raise PlanRefused("An entry at %s is at or after your flat-by time %s." % (entry_time, flat_by))

    reward_risk = round((target - entry) / risk, 2)
    result = {
        "symbol": symbol,
        "display": universe.display(symbol),
        "name": universe.names().get(symbol, symbol),
        "side": "long",
        "setup": setup,
        "date": clock.now_ist().strftime("%Y-%m-%d"),
        "entry": round(entry, 2),
        "entry_time": entry_time,
        "entry_bar": entry_bar,
        "stop": stop,
        "stop_rule": stop_rule,
        "stop_why": stop_why,
        "target": target,
        "target_rule": target_rule,
        "target_r": target_r,
        "target_why": target_why,
        "risk_per_share": risk,
        "risk_pct": round(risk / entry * 100, 2),
        "reward_risk": reward_risk,
        "flat_by": flat_by,
        "opening_range": ({"high": or_high, "low": or_low} if or_high and or_low else None),
        "min_reward_risk": cfg.get("min_reward_risk", 1.0),
        "as_of": clock.fmt_dt(clock.now_ist()),
        "note": ("A stop is a plan, not a guarantee: in a fast market or across a gap it can fill "
                 "well past its price. Flat by %s either way, no overnight hold." % flat_by),
    }
    if reward_risk < cfg.get("min_reward_risk", 1.0):
        result["warning"] = ("Reward:risk is %.2f, under your minimum %.2f. The Risk Manager will "
                             "block this." % (reward_risk, cfg.get("min_reward_risk", 1.0)))
    if write:
        store.write("plan.json", result)
    return result


def _stop_from_rule(symbol: str, entry: float, rule: str, or_high, or_low):
    rule = (rule or "").strip()
    if rule == "or-mid":
        if or_high is None or or_low is None:
            raise PlanRefused("An opening-range stop needs the range. The level setup has none: "
                              "name a stop instead (a price, a percent or an ATR multiple).")
        mid = (float(or_high) + float(or_low)) / 2
        return mid, ("opening-range midpoint: (%.2f + %.2f) / 2 = %.3f, rounded down"
                     % (float(or_high), float(or_low), mid))
    if rule == "swing-low":
        saved = store.read("entry.json") or {}
        hint = saved.get("suggested_stop") if saved.get("symbol") == symbol else None
        if hint is None:
            raise PlanRefused("A swing-low stop needs the Entry Timer's suggested stop, and "
                              "there is none for this symbol. Run the entry check first.")
        return float(hint), ("the low since the break, %.2f (the level the reclaim fails at)"
                             % float(hint))
    if rule == "or-low":
        if or_low is None:
            raise PlanRefused("An opening-range stop needs the range low.")
        return float(or_low), "the opening-range low %.2f" % float(or_low)
    if rule.startswith("price:"):
        value = float(rule.split(":", 1)[1])
        return value, "your price %.2f" % value
    if rule.startswith("pct:"):
        pct = float(rule.split(":", 1)[1])
        value = entry * (1 - pct / 100)
        return value, "%.2f%% under the entry %.2f" % (pct, entry)
    if rule.startswith("atr:"):
        mult = float(rule.split(":", 1)[1])
        bars = data.session_bars(symbol, interval="5m")
        value_atr = scanner.atr(bars, 14)
        if not value_atr:
            raise PlanRefused("Not enough bars yet for an ATR stop.")
        used = min(14, max(len(bars) - 1, 1))
        return entry - mult * value_atr, ("%.2f x the %d-bar ATR (%.2f) under the entry"
                                          % (mult, used, value_atr))
    raise PlanRefused("Unknown stop rule %r. Use or-mid, or-low, price:X, pct:X or atr:K." % rule)


def _target_from_rule(entry: float, risk: float, rule: str):
    rule = (rule or "2R").strip()
    if rule.lower().startswith("price:"):
        value = float(rule.split(":", 1)[1])
        r = round((value - entry) / risk, 2)
        return value, "your price %.2f (%.2fR)" % (value, r), r
    text = rule.upper().rstrip("R")
    try:
        r = float(text)
    except ValueError:
        raise PlanRefused("Unknown target rule %r. Use 1R, 1.5R, 2R or price:X." % rule)
    if r <= 0:
        raise PlanRefused("A target must be above the entry: use a positive R.")
    label = "%gR (%g times the %.2f risk a share)" % (r, r, risk)
    return entry + r * risk, label, r
