"""Movers: which NIFTY names actually swing, and how dependably.

A day trade needs room to move. A stock whose whole session fits inside 0.6 %
cannot pay for a stop, a spread and the charges, however good the setup looks.
This ranks the universe on what its own last few weeks of 5-minute bars did.

What it measures, per name, over the last N whole sessions:

* **day range**   mean (session high - session low) / session open, in percent.
* **steadiness**  1 - stdev(range) / mean(range). High means the range turns up
                  day after day instead of coming from one wild session.
* **swing**       1 - efficiency ratio: net move over the session divided by the
                  distance actually travelled, flipped. High means it goes up and
                  down a lot and ends near where it started. Low means it trends.
* **turns**       how many times the 5-minute closes change direction in a day.
* **per-bar**     mean absolute 5-minute move, in percent.
* **the setup's own record**: the opening-range breakout re-run on each of those
  sessions with an or-mid stop and a 2R target -- how often it fired, hit the
  target, or hit the stop.

The score is `day range x (0.5 + 0.5 x steadiness) x (0.5 + 0.5 x swing)`, which
is nothing cleverer than "it moves, both ways, and it keeps doing it".

**It is not a prediction, and nothing here is predictable.** A stock that swung
2 % a day for a month can sit flat tomorrow, and a high score says only that
there has been room to work with, not that a trade in it will win. The setup's
own record on those same sessions is printed next to the score precisely so the
number cannot be read as a promise.
"""
from __future__ import annotations

import math

from . import clock, data, scanner, store, universe

MIN_BARS = 60  # a session has 75; allow for Yahoo cutting the tail short


def _sessions(symbol: str, want: int):
    grouped = data.sessions_60d(symbol)
    today = clock.now_ist().strftime("%Y-%m-%d")
    out = []
    for day in sorted(grouped):
        if day >= today:
            continue
        bars = grouped[day]
        if len(bars) < MIN_BARS:
            continue
        if bars[0]["start"][-5:] != clock.SESSION_OPEN.strftime("%H:%M"):
            continue
        out.append((day, bars))
    return out[-want:]


def _stdev(values: list) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def profile(symbol: str, want_sessions: int = 20, or_minutes: int = 15) -> dict | None:
    symbol = universe.normalise(symbol)
    sessions = _sessions(symbol, want_sessions)
    if len(sessions) < 5:
        return None

    ranges, swings, turns, bar_moves, turnovers, gaps = [], [], [], [], [], []
    fired = hit_target = hit_stop = 0
    prev_close = None

    for _day, bars in sessions:
        opens = bars[0]["o"]
        high = max(b["h"] for b in bars)
        low = min(b["l"] for b in bars)
        closes = [b["c"] for b in bars]
        if not opens:
            continue
        ranges.append((high - low) / opens * 100)
        if prev_close:
            gaps.append(abs(opens - prev_close) / prev_close * 100)
        prev_close = closes[-1]

        steps = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
        travelled = sum(steps)
        net = abs(closes[-1] - closes[0])
        swings.append(1 - (net / travelled) if travelled else 0.0)
        bar_moves.append((travelled / len(steps)) / opens * 100 if steps else 0.0)

        flips = 0
        last_sign = 0
        for i in range(1, len(closes)):
            sign = (closes[i] > closes[i - 1]) - (closes[i] < closes[i - 1])
            if sign and last_sign and sign != last_sign:
                flips += 1
            if sign:
                last_sign = sign
        turns.append(flips)
        turnovers.append(sum(b["c"] * b["v"] for b in bars))

        # The opening-range breakout, re-run on this session: or-mid stop, 2R target.
        rng = scanner.opening_range(bars, or_minutes)
        if not rng:
            continue
        count = rng["bars"]
        entry_index = next((i for i, b in enumerate(bars[count:], start=count)
                            if b["c"] > rng["high"]), None)
        if entry_index is None:
            continue
        entry = bars[entry_index]["c"]
        stop = math.floor(((rng["high"] + rng["low"]) / 2) * 100) / 100
        risk = entry - stop
        if risk <= 0:
            continue
        fired += 1
        target = entry + 2 * risk
        for bar in bars[entry_index + 1:]:
            if bar["l"] <= stop:
                hit_stop += 1
                break
            if bar["h"] >= target:
                hit_target += 1
                break

    if not ranges:
        return None
    mean_range = sum(ranges) / len(ranges)
    steadiness = max(0.0, 1 - (_stdev(ranges) / mean_range)) if mean_range else 0.0
    swing = sum(swings) / len(swings) if swings else 0.0
    score = mean_range * (0.5 + 0.5 * steadiness) * (0.5 + 0.5 * swing)

    return {
        "symbol": symbol,
        "display": universe.display(symbol),
        "name": universe.names().get(symbol, symbol),
        "sessions": len(sessions),
        "from": sessions[0][0],
        "to": sessions[-1][0],
        "day_range_pct": round(mean_range, 2),
        "range_best": round(max(ranges), 2),
        "range_worst": round(min(ranges), 2),
        "steadiness": round(steadiness, 2),
        "swing": round(swing, 2),
        "turns": round(sum(turns) / len(turns)) if turns else 0,
        "bar_move_pct": round(sum(bar_moves) / len(bar_moves), 3) if bar_moves else 0,
        "gap_pct": round(sum(gaps) / len(gaps), 2) if gaps else None,
        "turnover": round(sum(turnovers) / len(turnovers)) if turnovers else 0,
        "last": sessions[-1][1][-1]["c"],
        "orb_fired": fired,
        "orb_target": hit_target,
        "orb_stop": hit_stop,
        "orb_neither": fired - hit_target - hit_stop,
        "orb_target_rate": round(hit_target / fired * 100, 1) if fired else None,
        "score": round(score, 2),
    }


def rank(group: str = "nifty50", want_sessions: int = 20, min_turnover: float = 0.0,
         or_minutes: int | None = None, write: bool = True) -> dict:
    cfg = store.config()
    or_minutes = or_minutes or int(cfg.get("or_minutes", 15))
    symbols = universe.symbols(group)
    rows, failed = [], []
    for symbol in symbols:
        try:
            row = profile(symbol, want_sessions, or_minutes)
        except data.DataError:
            row = None
        if not row:
            failed.append(universe.display(symbol))
            continue
        if row["turnover"] < min_turnover:
            continue
        rows.append(row)
    rows.sort(key=lambda r: r["score"], reverse=True)
    for i, row in enumerate(rows, start=1):
        row["rank"] = i

    out = {
        "as_of": clock.fmt_dt(clock.now_ist()),
        "universe": group,
        "sessions": want_sessions,
        "or_minutes": or_minutes,
        "ranked": len(rows),
        "failed": failed,
        "rows": rows,
        "note": ("Ranked on what these names did over their own last %d sessions of 5-minute "
                 "bars: how much ground they covered in a day, how dependably, and how much of "
                 "it was back-and-forth rather than one direction. The opening-range columns are "
                 "that same setup re-run on those sessions with an or-mid stop and a 2R target."
                 % want_sessions),
        "caveat": ("A high score means there has been room to trade, not that the next session "
                   "will move the same way or that a trade will win. Nothing here is a "
                   "prediction, and no screen makes a stock predictable."),
    }
    if write:
        store.write("movers.json", out)
    return out
