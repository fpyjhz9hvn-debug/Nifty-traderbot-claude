"""Time Machine (trader-timemachine, ported to NSE).

Before anything is sized or sent, one question: if today plays out like this
stock's own last 60 days, how often does this plan hit the target first, the
stop first, or neither?

Two answers. 10,000 replays of the rest of the session, each bar drawn from the
stock's own bars at that time of day. And the real record: the same rule re-run
on every past session in the pool.

It is a simulation built from 60 days of 5-minute bars, not a prediction. The
future can do what those 60 days never did. The seed (19) is printed with every
run so any result can be repeated exactly.
"""
from __future__ import annotations

import datetime as _dt
import math
import random

from . import clock, data, exitplan, scanner, store, universe

DEFAULT_SEED = 19
DEFAULT_REPLAYS = 10000
SLOT_WINDOW = 2


def _slot_of(bar: dict) -> int:
    hhmm = bar["start"][-5:]
    minutes = int(hhmm[:2]) * 60 + int(hhmm[3:])
    open_min = clock.SESSION_OPEN.hour * 60 + clock.SESSION_OPEN.minute
    return (minutes - open_min) // clock.BAR_MINUTES


def _full_sessions(grouped: dict, before_date: str, min_last_slot: int) -> dict:
    """Whole sessions before the plan's date: no gaps, and long enough to replay.

    Yahoo's NSE 5-minute feed usually stops at 15:15 or 15:20 rather than 15:30,
    so a session counts as whole when it starts at the 09:15 bar, has no missing
    bar in between, and reaches at least the bar the replay ends on. A day with a
    halt or a missing bar is left out, and so is today.
    """
    out = {}
    for day, bars in grouped.items():
        if day >= before_date:
            continue
        if not bars or _slot_of(bars[0]) != 0:
            continue
        if _slot_of(bars[-1]) < min_last_slot or len(bars) <= min_last_slot:
            continue
        # No missing bar anywhere the replay will walk. Yahoo drops the odd late
        # bar; a gap before the cut-off means a halt or a thin book, so skip it.
        if any(_slot_of(bars[i]) != i for i in range(min_last_slot + 1)):
            continue
        out[day] = bars
    return dict(sorted(out.items()))


def _ratio_pool(sessions: dict) -> dict:
    """slot -> [(o,h,l,c) as ratios to the previous close]."""
    pool: dict = {}
    for bars in sessions.values():
        for i in range(1, len(bars)):
            prev = bars[i - 1]["c"]
            if not prev:
                continue
            bar = bars[i]
            pool.setdefault(_slot_of(bar), []).append(
                (bar["o"] / prev, bar["h"] / prev, bar["l"] / prev, bar["c"] / prev))
    return pool


def _draw_slots(pool: dict, slot: int) -> list:
    picks = []
    for s in range(slot - SLOT_WINDOW, slot + SLOT_WINDOW + 1):
        picks.extend(pool.get(s, ()))
    return picks or pool.get(slot, [])


def _percentile(sorted_values: list, pct: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * pct
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_values[int(k)]
    return sorted_values[lo] * (hi - k) + sorted_values[hi] * (k - lo)


def _replay_history(sessions: dict, plan: dict, or_minutes: int, until_slot: int) -> dict:
    """The plan's own rule, re-run on every past session. What really happened."""
    fired = targets = stops = neither = 0
    r_total = 0.0
    rows = []
    count = max(1, or_minutes // clock.BAR_MINUTES)
    for day, bars in sessions.items():
        rng = scanner.opening_range(bars, or_minutes)
        if not rng:
            continue
        entry_index = None
        for index, bar in enumerate(bars[count:], start=count):
            if bar["c"] > rng["high"]:
                entry_index = index
                break
        if entry_index is None or entry_index >= until_slot:
            continue
        entry = bars[entry_index]["c"]
        try:
            stop, _ = exitplan._stop_from_rule(plan["symbol"], entry, plan["stop_rule"],
                                               rng["high"], rng["low"])
        except Exception:  # noqa: BLE001
            continue
        stop = math.floor(stop * 100) / 100
        risk = entry - stop
        if risk <= 0:
            continue
        target = entry + (plan.get("target_r") or 2.0) * risk
        fired += 1
        outcome, final = "neither", entry
        for bar in bars[entry_index + 1: until_slot + 1]:
            final = bar["c"]
            if bar["l"] <= stop:
                outcome = "stop"
                break
            if bar["h"] >= target:
                outcome = "target"
                break
        if outcome == "target":
            targets += 1
            r = plan.get("target_r") or 2.0
        elif outcome == "stop":
            stops += 1
            r = -1.0
        else:
            neither += 1
            r = (final - entry) / risk
        r_total += r
        rows.append({"date": day, "entry_bar": entry_index + 1, "entry": round(entry, 2),
                     "stop": round(stop, 2), "target": round(target, 2),
                     "outcome": outcome, "r": round(r, 2)})
    return {
        "sessions": len(sessions),
        "fired": fired,
        "target": targets,
        "stop": stops,
        "neither": neither,
        "avg_r": round(r_total / fired, 3) if fired else None,
        "rows": rows[-40:],
    }


def _verdict(target_pct: float, stop_pct: float, target_r: float) -> str:
    decided = target_pct + stop_pct
    if decided <= 0:
        return "No replay hit either side: the price at the cut-off decides every one of them."
    share = target_pct / decided * 100
    breakeven = 100.0 / (1.0 + target_r)
    edge = share - breakeven
    coin = "Close to a coin flip" if abs(target_r - 1.0) < 1e-9 else "Close to break-even"
    if abs(edge) < 5:
        head = coin
    elif abs(edge) < 15:
        head = "Leans toward the target" if edge > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if edge > 0 else "Strongly toward the stop"
    return ("%s: target first %.1f %% vs stop first %.1f %%. Of the replays that hit a side, the "
            "target came first %.1f %%; a %gR target needs %.1f %% to break even."
            % (head, target_pct, stop_pct, share, target_r, breakeven))


def run(plan: dict | None = None, replays: int = DEFAULT_REPLAYS, seed: int = DEFAULT_SEED,
        until: str | None = None, write: bool = True) -> dict:
    plan = plan or store.read("plan.json")
    if not plan:
        raise ValueError("No plan to replay. Run the Exit Planner first.")
    cfg = store.config()
    symbol = universe.normalise(plan["symbol"])
    or_minutes = int(cfg.get("or_minutes", 15))
    until = until or plan.get("flat_by") or cfg.get("flat_by", "15:20")

    until_time = clock.parse_hhmm(until)
    open_min = clock.SESSION_OPEN.hour * 60 + clock.SESSION_OPEN.minute
    until_slot = min(clock.BARS_PER_SESSION - 1,
                     ((until_time.hour * 60 + until_time.minute) - open_min)
                     // clock.BAR_MINUTES - 1)

    grouped = data.sessions_60d(symbol)
    today = plan.get("date") or clock.now_ist().strftime("%Y-%m-%d")
    sessions = _full_sessions(grouped, today, until_slot)
    if len(sessions) < 20:
        # Yahoo can cut a session short; fall back to days that reach the entry
        # bar at least, and say so in the result.
        sessions = _full_sessions(grouped, today, max(0, until_slot - 6))
    if len(sessions) < 20:
        raise ValueError("Only %d whole past sessions of 5-minute bars for %s: too few for a fair "
                         "pool (20 needed)." % (len(sessions), universe.display(symbol)))

    entry_bar = plan.get("entry_bar")
    if not entry_bar and plan.get("entry_time"):
        t = clock.parse_hhmm(plan["entry_time"])
        entry_bar = ((t.hour * 60 + t.minute) - open_min) // clock.BAR_MINUTES
    entry_slot = max(0, int(entry_bar or 1) - 1)
    bars_left = until_slot - entry_slot
    if bars_left < 1:
        raise ValueError("The entry at %s leaves no bars before %s: nothing to replay."
                         % (plan.get("entry_time"), until))

    entry = float(plan["entry"])
    stop = float(plan["stop"])
    target = float(plan["target"])
    risk = entry - stop
    target_r = float(plan.get("target_r") or round((target - entry) / risk, 2))

    pool = _ratio_pool(sessions)
    rng_random = random.Random(seed)
    draws = {s: _draw_slots(pool, s) for s in range(entry_slot + 1, until_slot + 1)}
    if not all(draws.values()):
        raise ValueError("The 60-day pool has a gap at one of the remaining times of day.")

    hit_target = hit_stop = hit_neither = green = 0
    r_values, finals, samples = [], [], []
    band_prices = {s: [] for s in draws}

    for n in range(replays):
        price = entry
        outcome, final = "neither", entry
        path = [round(entry, 2)] if n < 160 else None
        for slot in range(entry_slot + 1, until_slot + 1):
            o_r, h_r, l_r, c_r = rng_random.choice(draws[slot])
            high, low, close = price * h_r, price * l_r, price * c_r
            band_prices[slot].append(close)
            if path is not None:
                path.append(round(close, 2))
            if low <= stop:
                outcome, final = "stop", stop
                break
            if high >= target:
                outcome, final = "target", target
                break
            price = close
            final = close
        if outcome == "target":
            hit_target += 1
            r = target_r
            green += 1
        elif outcome == "stop":
            hit_stop += 1
            r = -1.0
        else:
            hit_neither += 1
            r = (final - entry) / risk
            if final > entry:
                green += 1
        r_values.append(r)
        finals.append(final)
        if path is not None:
            samples.append({"outcome": outcome, "path": path})

    finals_sorted = sorted(finals)
    pct = lambda n: round(n / replays * 100, 1)  # noqa: E731
    bands = []
    for slot in sorted(band_prices):
        values = sorted(band_prices[slot])
        if not values:
            continue
        bands.append({
            "slot": slot,
            "time": clock.fmt_time(clock.session_bounds(clock.now_ist().date())[0]
                                   + _dt.timedelta(minutes=(slot + 1) * clock.BAR_MINUTES)),
            "p5": round(_percentile(values, 0.05), 2),
            "p25": round(_percentile(values, 0.25), 2),
            "p50": round(_percentile(values, 0.50), 2),
            "p75": round(_percentile(values, 0.75), 2),
            "p95": round(_percentile(values, 0.95), 2),
        })

    lo, hi = finals_sorted[0], finals_sorted[-1]
    bins, width = [], max((hi - lo) / 24, 0.01)
    counts = [0] * 24
    for value in finals_sorted:
        counts[min(23, int((value - lo) / width))] += 1
    for i, count in enumerate(counts):
        bins.append({"from": round(lo + i * width, 2), "to": round(lo + (i + 1) * width, 2),
                     "count": count})

    mc = {
        "replays": replays,
        "seed": seed,
        "until": until,
        "bars_replayed": bars_left,
        "target_first_pct": pct(hit_target),
        "stop_first_pct": pct(hit_stop),
        "neither_pct": pct(hit_neither),
        "green_pct": pct(green),
        "avg_r": round(sum(r_values) / replays, 3),
        "median_close": round(_percentile(finals_sorted, 0.50), 2),
        "p5_close": round(_percentile(finals_sorted, 0.05), 2),
        "p95_close": round(_percentile(finals_sorted, 0.95), 2),
        "bands": bands,
        "samples": samples,
        "histogram": bins,
    }
    history = _replay_history(sessions, {**plan, "symbol": symbol, "target_r": target_r},
                             or_minutes, until_slot)
    verdict = _verdict(mc["target_first_pct"], mc["stop_first_pct"], target_r)
    notes = []
    if mc["neither_pct"] > 40:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (mc["neither_pct"], until, until))
    leaning_target = mc["target_first_pct"] >= mc["stop_first_pct"]
    if (mc["avg_r"] > 0) != leaning_target:
        notes.append("The average R (%+.3f) points the other way from the lean, because of the "
                     "replays that hit neither." % mc["avg_r"])
    if history["fired"] and history["avg_r"] is not None:
        notes.append("History and the replays %s."
                     % ("agree" if (history["avg_r"] > 0) == (mc["avg_r"] > 0) else "disagree"))

    out = {
        "plan": {k: plan.get(k) for k in ("symbol", "display", "entry", "entry_time", "entry_bar",
                                          "stop", "target", "target_r", "risk_per_share",
                                          "stop_rule", "setup", "date")},
        "pool_sessions": len(sessions),
        "pool_from": min(sessions) if sessions else None,
        "pool_to": max(sessions) if sessions else None,
        "mc": mc,
        "history": history,
        "verdict": verdict,
        "notes": notes,
        "as_of": clock.fmt_dt(clock.now_ist()),
        "disclaimer": ("Built from %s's own last 60 days of 5-minute bars, seed %d. A simulation, "
                       "not a prediction: news, a halt or a gap can do what those 60 days never "
                       "did." % (universe.display(symbol), seed)),
    }
    if write:
        store.write("timemachine.json", out)
    return out
