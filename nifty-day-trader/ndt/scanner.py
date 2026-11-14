"""Chart Scanner (trader-scanner, ported to NSE).

Two scans:

* `daily`  -- before the open, on daily closes: 20-day closing-high breakout,
  bull flag, reclaimed the 20-day average, oversold (RSI 14 Wilder under 30),
  oversold bounce.
* `orb`    -- during the session, on 5-minute bars: the first completed bar to
  CLOSE above the opening-range high.

A scan is a list of charts that matched a rule. It is not a list of stocks to
buy, and a setup on a chart is not a prediction.
"""
from __future__ import annotations

import datetime as _dt

from . import clock, data, store, universe


# ------------------------------------------------------------------ indicators

def rsi14(closes: list, period: int = 14):
    """Wilder's RSI on closes."""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def sma(values: list, period: int):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def atr(bars: list, period: int = 14):
    """Average true range over the last `period` bars (all of them if fewer)."""
    if len(bars) < 2:
        return None
    trs = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1]["c"]
        high, low = bars[i]["h"], bars[i]["l"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    use = trs[-period:] if len(trs) >= period else trs
    return round(sum(use) / len(use), 2) if use else None


# ----------------------------------------------------------------- daily setups

def _daily_setups(closes: list) -> list:
    """Which rules this series of daily closes matches. Closes only."""
    hits = []
    if len(closes) < 22:
        return hits
    last = closes[-1]
    prev20 = closes[-21:-1]
    if last > max(prev20):
        hits.append(("breakout_20d", "closed above every close of the 20 sessions before it"))
    ma20_now = sma(closes, 20)
    ma20_prev = sma(closes[:-1], 20)
    if ma20_now and ma20_prev and closes[-2] < ma20_prev and last > ma20_now:
        hits.append(("reclaim_ma20", "closed back above its 20-day average (%.2f)" % ma20_now))
    if len(closes) >= 16:
        base, pole_end = closes[-16], closes[-6]
        window = closes[-5:]
        if base > 0 and (pole_end - base) / base > 0.10:
            lo, hi = min(window), max(window)
            if lo > 0 and (hi - lo) / lo < 0.05:
                hits.append(("bull_flag",
                             "up %.1f%% over 10 sessions, then 5 closes inside a %.1f%% range"
                             % ((pole_end - base) / base * 100, (hi - lo) / lo * 100)))
    r_now = rsi14(closes)
    r_prev = rsi14(closes[:-1])
    if r_now is not None and r_now < 30:
        hits.append(("oversold", "RSI 14 is %.1f, under 30" % r_now))
    if r_prev is not None and r_prev < 30 and last > closes[-2]:
        hits.append(("oversold_bounce", "RSI 14 was %.1f yesterday and today closed up" % r_prev))
    return hits


def scan_daily(group: str = "nifty50", min_price: float = 0.0, write: bool = True) -> dict:
    """The pre-market scan over daily closes."""
    symbols = universe.symbols(group)
    series = data.daily_closes(symbols, rng="3mo")
    names = universe.names()
    hits, scanned, skipped = [], 0, 0
    for sym in symbols:
        row = series.get(sym)
        if not row or len(row["closes"]) < 22:
            skipped += 1
            continue
        if row["price"] and row["price"] < min_price:
            skipped += 1
            continue
        scanned += 1
        closes = row["closes"]
        matches = _daily_setups(closes)
        if not matches:
            continue
        turnover = (row["price"] or closes[-1]) * (row["volume"] or 0)
        for setup, why in matches:
            hits.append({
                "symbol": sym,
                "display": universe.display(sym),
                "name": names.get(sym, sym),
                "setup": setup,
                "why": why,
                "price": row["price"] or closes[-1],
                "prev_close": closes[-2],
                "change_pct": round(((closes[-1] - closes[-2]) / closes[-2]) * 100, 2) if closes[-2] else 0,
                "rsi14": rsi14(closes),
                "ma20": round(sma(closes, 20), 2) if sma(closes, 20) else None,
                "turnover": turnover,
            })
    hits.sort(key=lambda h: h["turnover"], reverse=True)
    out = {
        "mode": "daily",
        "as_of": clock.now_ist().strftime("%Y-%m-%d %H:%M"),
        "universe": group,
        "symbols": len(symbols),
        "fetched": len(series),
        "scanned": scanned,
        "skipped": skipped,
        "hits": hits,
        "note": ("Daily closes from Yahoo's free spark endpoint. Today's own daily bar is only "
                 "complete after the close. Each hit matched a rule; none of them is a buy."),
    }
    if write:
        store.write("watchlist.json", out)
    return out


# -------------------------------------------------------- opening-range breakout

def opening_range(bars: list, or_minutes: int = 15) -> dict | None:
    """High and low of the first `or_minutes` of the session."""
    count = max(1, or_minutes // clock.BAR_MINUTES)
    if len(bars) < count:
        return None
    window = bars[:count]
    first_start = window[0]["start"][11:]
    if first_start != clock.SESSION_OPEN.strftime("%H:%M"):
        return None  # the session's first bar is missing: a halt or a thin name
    return {
        "high": max(b["h"] for b in window),
        "low": min(b["l"] for b in window),
        "max_close": max(b["c"] for b in window),
        "bars": count,
        "avg_volume": sum(b["v"] for b in window) / count,
        "from": window[0]["start"][11:],
        "to": window[-1]["end"],
    }


def _orb_hit(bars: list, or_minutes: int = 15):
    """The first completed bar after the range that CLOSES above the range high."""
    rng = opening_range(bars, or_minutes)
    if not rng:
        return None, None
    count = rng["bars"]
    for index, bar in enumerate(bars[count:], start=count):
        if bar["c"] > rng["high"]:
            return rng, {
                "bar_index": index + 1,
                "trigger_time": bar["end"],
                "price": bar["c"],
                "over_pct": round((bar["c"] - rng["high"]) / rng["high"] * 100, 2),
                "volume": bar["v"],
                "volume_x": round(bar["v"] / rng["avg_volume"], 2) if rng["avg_volume"] else None,
            }
    return rng, None


def scan_orb(group: str = "nifty50", or_minutes: int = 15, min_price: float = 0.0,
             write: bool = True) -> dict:
    """The intraday opening-range breakout scan, two stages like the skill."""
    cfg = store.config()
    now = clock.now_ist()
    if not clock.is_trading_day(now.date(), store.holidays()):
        return {"mode": "orb", "error": "The NSE is closed today (%s)." % now.strftime("%A"),
                "hits": [], "as_of": clock.fmt_dt(now)}
    earliest = clock.session_bounds(now.date())[0] + _dt.timedelta(minutes=or_minutes + 5)
    if now < earliest:
        return {"mode": "orb", "hits": [], "as_of": clock.fmt_dt(now),
                "error": "The %d-minute opening range is still forming; come back after %s IST."
                         % (or_minutes, clock.fmt_time(earliest))}

    symbols = universe.symbols(group)
    names = universe.names()
    stage1 = data.intraday_spark(symbols, "5m")
    count = max(1, or_minutes // clock.BAR_MINUTES)

    candidates, usable, failed = [], 0, []
    for sym in symbols:
        row = stage1.get(sym)
        if not row or len(row["bars"]) <= count:
            failed.append(sym)
            continue
        bars = row["bars"]
        if bars[0]["start"] != clock.SESSION_OPEN.strftime("%H:%M"):
            failed.append(sym)
            continue
        usable += 1
        if row["price"] and row["price"] < min_price:
            continue
        max_range_close = max(b["c"] for b in bars[:count])
        if any(b["c"] > max_range_close for b in bars[count:]):
            candidates.append((sym, (row["price"] or 0) * (row["volume"] or 0)))

    candidates.sort(key=lambda c: c[1], reverse=True)
    hits, confirmed = [], 0
    for sym, _turnover in candidates:
        try:
            bars = data.session_bars(sym, interval="5m")
        except data.DataError:
            failed.append(sym)
            continue
        confirmed += 1
        rng, hit = _orb_hit(bars, or_minutes)
        if not rng or not hit:
            continue
        hits.append({
            "symbol": sym,
            "display": universe.display(sym),
            "name": names.get(sym, sym),
            "setup": "orb",
            "trigger_time": hit["trigger_time"],
            "price": hit["price"],
            "or_high": rng["high"],
            "or_low": rng["low"],
            "over_pct": hit["over_pct"],
            "volume_x": hit["volume_x"],
            "bar_index": hit["bar_index"],
            "why": "the %s bar closed %.2f%% above the opening-range high %.2f"
                   % (hit["trigger_time"], hit["over_pct"], rng["high"]),
            "last": bars[-1]["c"] if bars else hit["price"],
        })
    hits.sort(key=lambda h: h["trigger_time"])
    out = {
        "mode": "orb",
        "as_of": clock.fmt_dt(now),
        "universe": group,
        "or_minutes": or_minutes,
        "symbols": len(symbols),
        "usable": usable,
        "candidates": len(candidates),
        "confirmed": confirmed,
        "failed": len(failed),
        "hits": hits,
        "cutoff": cfg["no_entries_after"],
        "note": ("Stage 1 reads 5-minute closes for every name, stage 2 re-checks each candidate "
                 "with real highs and lows. A hit is the first completed 5-minute bar to close "
                 "above the opening-range high. A hit from an hour ago is stale: the price has "
                 "moved on."),
    }
    if write:
        store.write("watchlist.json", out)
    return out
