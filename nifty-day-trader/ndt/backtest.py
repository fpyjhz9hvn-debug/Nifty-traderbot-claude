"""Does the discipline layer actually help? Measure it, don't assume it.

`backtest-expert` and `docs/dev/strategy-research-bias-checklist.md` in
github.com/tradermonty/claude-trading-skills make the same point twice: a rule
you have not measured is a belief, and a backtest you tuned until it looked good
is a story about the past. So this runs the app's own opening-range breakout
over every session in the 60-day pool, across the universe, and reports the
result with and without each gate switched on.

What it does:

* For each past session and each symbol, take the 15-minute opening range, enter
  on the first 5-minute bar to CLOSE above the range high, stop at the range
  midpoint, target at 2R, flat by the configured time.
* Walk the remaining bars: the stop is checked before the target, and a bar that
  touches both counts as the stop -- the pessimistic assumption.
* Cap entries per session the way the live rules do (trades per day).
* Count the result in R, so results are comparable across prices, and in rupees
  net of the same estimated charges the paper broker applies.
* Re-run the whole thing with the regime filter on: a session only trades when
  the NIFTY's own state THAT DAY allowed new entries.

What it cannot do: tell you what tomorrow does. It is one universe, one setup,
one 60-day window, and 60 days is a small sample however many trades come out of
it. Both arms use exactly the same entries and exits, so the comparison between
them is fair even where the absolute numbers are not meaningful.

Known limits, stated rather than hidden:

* Fills are at the bar's close (entry) and at the level (stop/target). A real
  fill can be worse, so real results are worse than these.
* Survivorship: the universe is today's NIFTY list, used over a past window.
* No slippage, no partial fills, no queue position, no gap-through modelling.
"""
from __future__ import annotations

import math

from . import broker, clock, data, regime, scanner, store, universe


def size_like_the_app(entry: float, risk_per_share: float, cfg: dict) -> int:
    """The Budget Manager's own limits, so a replay sizes what the app would size.

    Risk-budget sizing alone gives a tight stop an enormous position, and in
    India the charges scale with turnover rather than with risk. Leaving the
    position cap and the max order value out of a replay therefore overstates
    the cost of exactly the setups that use tight stops.
    """
    budget = float(cfg["budget"])
    candidates = [
        math.floor(float(cfg["max_loss_per_trade"]) / risk_per_share),
        math.floor(budget * float(cfg["max_position_pct"]) / 100 / entry),
        math.floor(float(cfg["max_order_value"]) / entry),
    ]
    return max(0, min(candidates))


def _sessions(symbol: str):
    grouped = data.sessions_60d(symbol)
    today = clock.now_ist().strftime("%Y-%m-%d")
    out = {}
    for day, bars in grouped.items():
        if day >= today or len(bars) < 60:
            continue
        if bars[0]["start"][-5:] != clock.SESSION_OPEN.strftime("%H:%M"):
            continue
        out[day] = bars
    return out


def _trade_session(bars: list, or_minutes: int, target_r: float, until_slot: int,
                   entry_after_slot: int, stop_rule: str = "or-mid"):
    """One session, one symbol: the ORB trade or nothing."""
    rng = scanner.opening_range(bars, or_minutes)
    if not rng:
        return None
    count = rng["bars"]
    entry_index = None
    for i, bar in enumerate(bars[count:], start=count):
        if i < entry_after_slot:
            continue
        if i > until_slot:
            break
        if bar["c"] > rng["high"]:
            entry_index = i
            break
    if entry_index is None:
        return None
    entry = bars[entry_index]["c"]
    if stop_rule == "or-low":
        raw_stop = rng["low"]
    else:
        raw_stop = (rng["high"] + rng["low"]) / 2
    stop = math.floor(raw_stop * 100) / 100
    risk = entry - stop
    if risk <= 0 or risk / entry < 0.0005:
        return None
    target = entry + target_r * risk
    outcome, exit_price = "timed out", bars[min(until_slot, len(bars) - 1)]["c"]
    exit_index = min(until_slot, len(bars) - 1)
    for i in range(entry_index + 1, min(until_slot, len(bars) - 1) + 1):
        bar = bars[i]
        if bar["l"] <= stop:
            outcome, exit_price, exit_index = "stop", stop, i
            break
        if bar["h"] >= target:
            outcome, exit_price, exit_index = "target", target, i
            break
    r_multiple = (exit_price - entry) / risk
    return {
        "entry_index": entry_index, "entry_time": bars[entry_index]["end"],
        "entry": round(entry, 2), "stop": stop, "target": round(target, 2),
        "risk_per_share": round(risk, 2), "exit": round(exit_price, 2),
        "exit_time": bars[exit_index]["end"], "outcome": outcome,
        "r": round(r_multiple, 3),
    }


def _regime_by_day(write_back: bool = False) -> dict:
    """The regime state as it stood at the end of each past session.

    Only data up to that session is used, so a day is never judged with bars it
    could not have seen. That is the whole point of the exercise.
    """
    chart = data.chart(regime.INDEX, rng="6mo", interval="1d", ttl=900)
    bars = chart["bars"]
    out = {}
    for i in range(55, len(bars)):
        window = bars[:i + 1]
        closes = [b["c"] for b in window]
        last = closes[-1]
        ema21 = regime._ema(closes, 21)
        sma50 = regime._sma(closes, 50)
        below = bool((ema21 and last < ema21) or (sma50 and last < sma50))
        above_both = bool(ema21 and sma50 and last > ema21 and last > sma50)
        counts = regime.distribution_days(window)
        risk = regime._risk_level(counts, below)
        # Participation needs per-name history for that date, which the spark
        # feed does not give cheaply, so the day-level gate uses the index's own
        # trend and distribution risk. That makes this arm WEAKER than the live
        # gate, not stronger -- a conservative comparison.
        allows = above_both and risk in ("NORMAL", "CAUTION")
        out[window[-1]["date"]] = {
            "allows": allows, "risk": risk, "above_both": above_both,
            "d25": counts["d25"], "d5": counts["d5"],
        }
    return out


def _summarise(trades: list, label: str, opening: float) -> dict:
    if not trades:
        return {"label": label, "trades": 0, "note": "no trades"}
    wins = [t for t in trades if t["r"] > 0]
    losses = [t for t in trades if t["r"] < 0]
    total_r = sum(t["r"] for t in trades)
    avg_win_r = sum(t["r"] for t in wins) / len(wins) if wins else 0.0
    avg_loss_r = abs(sum(t["r"] for t in losses) / len(losses)) if losses else 0.0
    win_rate = len(wins) / len(trades)
    expectancy_r = total_r / len(trades)
    net = sum(t["net"] for t in trades)
    charges = sum(t["charges"] for t in trades)
    # Equity path by trade order, for a drawdown figure in R.
    peak = run = worst = 0.0
    for trade in trades:
        run += trade["r"]
        peak = max(peak, run)
        worst = max(worst, peak - run)
    by_outcome: dict = {}
    for trade in trades:
        by_outcome[trade["outcome"]] = by_outcome.get(trade["outcome"], 0) + 1
    return {
        "label": label,
        "trades": len(trades),
        "sessions_traded": len({t["date"] for t in trades}),
        "win_rate_pct": round(win_rate * 100, 1),
        "wins": len(wins), "losses": len(losses),
        "avg_win_r": round(avg_win_r, 3),
        "avg_loss_r": round(avg_loss_r, 3),
        "expectancy_r": round(expectancy_r, 4),
        "total_r": round(total_r, 2),
        "max_drawdown_r": round(worst, 2),
        "by_outcome": by_outcome,
        "net": round(net, 2),
        "charges": round(charges, 2),
        "net_pct_of_account": round(net / opening * 100, 2) if opening else None,
        "best": round(max(t["r"] for t in trades), 2),
        "worst": round(min(t["r"] for t in trades), 2),
    }


def run(group: str = "nifty50", target_r: float | None = None, max_trades_per_day: int | None = None,
        stop_rule: str = "or-mid", write: bool = True) -> dict:
    cfg = store.config()
    target_r = float(target_r or cfg.get("default_target_r", 2.0))
    max_trades_per_day = int(max_trades_per_day or cfg["max_trades_per_day"])
    or_minutes = int(cfg.get("or_minutes", 15))
    risk_budget = float(cfg["max_loss_per_trade"])
    opening = float(cfg["budget"])

    open_min = clock.SESSION_OPEN.hour * 60 + clock.SESSION_OPEN.minute
    flat = clock.parse_hhmm(cfg.get("flat_by", "15:20"))
    until_slot = min(clock.BARS_PER_SESSION - 1,
                     ((flat.hour * 60 + flat.minute) - open_min) // clock.BAR_MINUTES - 1)
    first = clock.parse_hhmm(cfg.get("no_entries_before", "09:30"))
    entry_after_slot = max(0, ((first.hour * 60 + first.minute) - open_min) // clock.BAR_MINUTES - 1)

    symbols = universe.symbols(group)
    per_day: dict = {}
    failed = []
    for symbol in symbols:
        try:
            sessions = _sessions(symbol)
        except data.DataError:
            failed.append(universe.display(symbol))
            continue
        for day, bars in sessions.items():
            trade = _trade_session(bars, or_minutes, target_r, until_slot, entry_after_slot,
                                   stop_rule)
            if not trade:
                continue
            trade.update({"symbol": symbol, "display": universe.display(symbol), "date": day})
            per_day.setdefault(day, []).append(trade)

    regimes = _regime_by_day()
    all_trades, gated_trades = [], []
    for day in sorted(per_day):
        taken = sorted(per_day[day], key=lambda t: (t["entry_index"], t["display"]))
        taken = taken[:max_trades_per_day]
        allowed = regimes.get(day, {}).get("allows")
        for trade in taken:
            qty = size_like_the_app(trade["entry"], trade["risk_per_share"], cfg)
            if qty < 1:
                continue
            gross = (trade["exit"] - trade["entry"]) * qty
            charges = (broker.charges("buy", qty, trade["entry"], cfg)["total"]
                       + broker.charges("sell", qty, trade["exit"], cfg)["total"])
            row = {**trade, "qty": qty, "gross": round(gross, 2),
                   "charges": round(charges, 2), "net": round(gross - charges, 2),
                   "regime_allowed": allowed}
            all_trades.append(row)
            if allowed:
                gated_trades.append(row)

    days = sorted(per_day)
    both = _summarise(all_trades, "every signal", opening)
    gated = _summarise(gated_trades, "only when the regime gate allowed new entries", opening)

    skipped_days = sorted(d for d in days if not regimes.get(d, {}).get("allows"))
    out = {
        "as_of": clock.fmt_dt(clock.now_ist()),
        "universe": group,
        "symbols": len(symbols),
        "failed": failed,
        "setup": {
            "rule": "opening-range breakout, %d-minute range, 5-minute bars" % or_minutes,
            "stop": "the opening-range low" if stop_rule == "or-low" else "the opening-range midpoint",
            "stop_rule": stop_rule,
            "target": "%gR" % target_r,
            "flat_by": cfg.get("flat_by", "15:20"),
            "entries_from": cfg.get("no_entries_before", "09:30"),
            "max_trades_per_day": max_trades_per_day,
            "risk_per_trade": risk_budget,
        },
        "sessions": len(days),
        "from": days[0] if days else None,
        "to": days[-1] if days else None,
        "ungated": both,
        "gated": gated,
        "regime_skipped_sessions": len(skipped_days),
        "regime_skipped_list": skipped_days,
        "verdict": _verdict(both, gated),
        "trades": sorted(all_trades, key=lambda t: (t["date"], t["entry_time"]))[-120:],
        "limits": [
            "Entry fills at the bar's close, stop and target fill AT the level. Real fills are "
            "worse, so real results are worse than these.",
            "The universe is today's NIFTY list applied to a past window, so names that left "
            "the index are missing (survivorship).",
            "No slippage, no partial fills, no queue position, no gap-through modelling.",
            "One setup, one universe, 60 days. A single window is not evidence of an edge, and "
            "nothing here was tuned -- the parameters are the app's own defaults.",
        ],
    }
    if write:
        store.write("backtest.json", out)
    return out


SWEEP_GRID = [("or-mid", 1.0), ("or-mid", 1.5), ("or-mid", 2.0), ("or-mid", 3.0),
              ("or-low", 1.0), ("or-low", 1.5), ("or-low", 2.0), ("or-low", 3.0)]


def sweep(group: str = "nifty50", write: bool = True) -> dict:
    """Every stop/target combination on the grid, all of them reported.

    The bias checklist in the skills repo is blunt about this: run a grid, keep
    the best cell and present it as "the strategy", and you have fitted noise.
    So the whole grid is reported, in one table, with the spread between best
    and worst shown -- because a parameter that only works in one cell is the
    cell, not an edge. Nothing here is auto-applied to the live config.
    """
    rows = []
    for stop_rule, target_r in SWEEP_GRID:
        result = run(group=group, target_r=target_r, stop_rule=stop_rule, write=False)
        for arm_key in ("ungated", "gated"):
            arm = result[arm_key]
            if not arm.get("trades"):
                continue
            rows.append({
                "stop": stop_rule, "target_r": target_r,
                "gate": arm_key == "gated",
                "trades": arm["trades"], "win_rate_pct": arm["win_rate_pct"],
                "expectancy_r": arm["expectancy_r"], "total_r": arm["total_r"],
                "max_drawdown_r": arm["max_drawdown_r"], "net": arm["net"],
                "charges": arm["charges"],
            })
    best = max(rows, key=lambda r: r["expectancy_r"]) if rows else None
    worst = min(rows, key=lambda r: r["expectancy_r"]) if rows else None
    positive = [r for r in rows if r["expectancy_r"] > 0]
    out = {
        "as_of": clock.fmt_dt(clock.now_ist()),
        "universe": group,
        "rows": rows,
        "best": best,
        "worst": worst,
        "cells_positive": len(positive),
        "cells": len(rows),
        "verdict": _sweep_verdict(rows, best, positive),
        "warning": ("This is an in-sample grid over one 60-day window. The whole grid is shown "
                    "on purpose. Picking the best cell and trading it is how a backtest becomes "
                    "a story about the past: with %d cells, the best one looks good partly "
                    "because it is the best of %d." % (len(rows), len(rows))),
    }
    if write:
        store.write("sweep.json", out)
    return out


def _sweep_verdict(rows, best, positive) -> str:
    if not rows:
        return "No cell produced a trade."
    if not positive:
        return ("Not one of the %d cells has positive expectancy on this window. The setup does "
                "not have an edge here at any stop/target on the grid, and changing the "
                "parameters does not create one." % len(rows))
    return ("%d of %d cells show positive expectancy. The best is a %s stop with a %gR target%s "
            "at %+.4fR a trade over %d trades. Treat that as the best of %d guesses on one "
            "window, not as a setting to trade."
            % (len(positive), len(rows), best["stop"], best["target_r"],
               " and the regime gate on" if best["gate"] else "", best["expectancy_r"],
               best["trades"], len(rows)))


def _verdict(ungated: dict, gated: dict) -> str:
    if not ungated.get("trades") or not gated.get("trades"):
        return "Not enough trades in the window to compare the two arms."
    a, b = ungated["expectancy_r"], gated["expectancy_r"]
    lead = ("Every signal: %d trades, %.1f%% win rate, %+.4fR a trade, %+.2fR in total, worst "
            "drawdown %.2fR. With the regime gate: %d trades, %.1f%% win rate, %+.4fR a trade, "
            "%+.2fR in total, worst drawdown %.2fR."
            % (ungated["trades"], ungated["win_rate_pct"], a, ungated["total_r"],
               ungated["max_drawdown_r"], gated["trades"], gated["win_rate_pct"], b,
               gated["total_r"], gated["max_drawdown_r"]))
    if b > a:
        tail = ("The gate improved expectancy by %.4fR a trade on this window, and it did it by "
                "not trading on %d of the sessions." % (b - a, ungated["sessions_traded"] -
                                                        gated["sessions_traded"]))
    elif b < a:
        tail = ("The gate made expectancy worse by %.4fR a trade on this window. That is the "
                "honest result; a filter that costs more than it saves should be turned off."
                % (a - b))
    else:
        tail = "The gate made no difference to expectancy on this window."
    if a < 0 and b < 0:
        tail += (" Both arms lose money on this window, so the question of which gate is better "
                 "comes second to the setup itself not having an edge here.")
    return lead + " " + tail
