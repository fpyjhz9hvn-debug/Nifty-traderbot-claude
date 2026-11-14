"""Entry Timer (trader-entry, ported to NSE).

One rule: the entry is the first COMPLETED bar after the opening range that
CLOSES above the level. A wick above is not an entry. A close exactly at the
level is not above it. The time is that bar's close time, the price its close.

Long side only, as in the skill pack. It never places anything.
"""
from __future__ import annotations

from . import clock, data, scanner, store, universe

STATUS_WAITING = "waiting"
STATUS_TRIGGERED = "triggered"
STATUS_EXPIRED = "expired"
STATUS_CLOSED = "market_closed"
STATUS_FORMING = "range_forming"
STATUS_NO_DATA = "no_data"


def check(symbol: str, setup: str = "orb", level: float | None = None,
          or_minutes: int | None = None, min_volume_x: float | None = None,
          interval: str = "5m", write: bool = False) -> dict:
    cfg = store.config()
    or_minutes = or_minutes or int(cfg.get("or_minutes", 15))
    symbol = universe.normalise(symbol)
    now = clock.now_ist()
    out = {
        "symbol": symbol,
        "display": universe.display(symbol),
        "name": universe.names().get(symbol, symbol),
        "setup": setup,
        "interval": interval,
        "as_of": clock.fmt_dt(now),
        "date": now.strftime("%Y-%m-%d"),
        "or_minutes": or_minutes,
        "min_volume_x": min_volume_x,
        "status": STATUS_NO_DATA,
    }

    if not clock.is_trading_day(now.date(), store.holidays()):
        out["status"] = STATUS_CLOSED
        out["message"] = "The NSE is closed today (%s). No entries." % now.strftime("%A")
        return out

    session_open, _ = clock.session_bounds(now.date())
    if now < session_open:
        out["status"] = STATUS_CLOSED
        out["message"] = "Not open yet. The session starts at 09:15 IST."
        return out

    try:
        bars = data.session_bars(symbol, interval=interval)
    except data.DataError as exc:
        out["message"] = str(exc)
        return out
    if not bars:
        out["message"] = "No bars for %s yet today." % universe.display(symbol)
        return out

    out["bars"] = len(bars)
    out["last_close"] = bars[-1]["c"]
    out["last_bar"] = bars[-1]["end"]

    if setup == "or_low_reclaim":
        # The failed breakdown: it loses the opening-range low, then closes back
        # above it. The one setup in ndt/setups.py that survived the design /
        # confirm split in ndt/research.py -- on one 60-day window, so it is
        # here to be paper-traded forward and watched, not believed.
        rng = scanner.opening_range(bars, or_minutes)
        if not rng:
            out["message"] = ("%s has no clean opening range, so there is no level to lose and "
                              "reclaim." % universe.display(symbol))
            return out
        out["opening_range"] = rng
        out["level"] = rng["low"]
        out["level_label"] = "the opening-range low"
        broke_at = None
        for index, bar in enumerate(bars[rng["bars"]:], start=rng["bars"]):
            if bar["c"] < rng["low"]:
                broke_at = broke_at if broke_at is not None else index
                continue
            if broke_at is not None and bar["c"] > rng["low"]:
                low_since = min(b["l"] for b in bars[rng["bars"]:index + 1])
                out["status"] = STATUS_TRIGGERED
                out["entry"] = bar["c"]
                out["entry_time"] = bar["end"]
                out["entry_bar"] = index + 1
                out["broke_at"] = bars[broke_at]["end"]
                out["suggested_stop"] = round(low_since, 2)
                out["over_pct"] = round((bar["c"] - rng["low"]) / rng["low"] * 100, 2)
                bars_ago = len(bars) - 1 - index
                out["bars_ago"] = bars_ago
                out["stale"] = bars_ago > 1
                out["message"] = ("TRIGGERED at %s, %.2f: the %s bar closed back above the "
                                  "opening-range low %.2f after losing it at %s. The suggested "
                                  "stop is the low since the break, %.2f. Nothing was placed."
                                  % (bar["end"], bar["c"], bar["end"], rng["low"],
                                     bars[broke_at]["end"], low_since))
                notes = []
                after = clock.parse_hhmm(cfg["no_entries_after"]) if cfg.get("no_entries_after") else None
                if after and clock.parse_hhmm(bar["end"]) > after:
                    notes.append("This fired after your %s entry cutoff." % cfg["no_entries_after"])
                    out["status"] = STATUS_EXPIRED
                if bars_ago > 1:
                    notes.append("The trigger is %d bars old; the last close is %.2f."
                                 % (bars_ago, bars[-1]["c"]))
                out["notes"] = notes
                if write and out["status"] == STATUS_TRIGGERED:
                    store.write("entry.json", out)
                return out
        out["status"] = STATUS_WAITING
        out["message"] = ("WAITING. %s has not lost and reclaimed its opening-range low %.2f yet "
                          "(last close %.2f)." % (universe.display(symbol), rng["low"],
                                                  bars[-1]["c"]))
        if broke_at is not None:
            out["message"] += " It is below the range low now, which is the first half of it."
        return out

    if setup == "level":
        if level is None:
            out["message"] = "A level setup needs a level to beat. Give it one."
            return out
        target_level, rng, label = float(level), None, "your level"
    else:
        rng = scanner.opening_range(bars, or_minutes)
        if not rng:
            count = max(1, or_minutes // clock.BAR_MINUTES)
            if len(bars) < count:
                out["status"] = STATUS_FORMING
                out["message"] = ("The %d-minute opening range is still forming (%d of %d bars closed)."
                                  % (or_minutes, len(bars), count))
            else:
                out["message"] = ("%s is missing its 09:15 bar, so there is no clean opening range "
                                  "(a halt or a thin book)." % universe.display(symbol))
            return out
        target_level, label = rng["high"], "the opening-range high"
        out["opening_range"] = rng

    out["level"] = round(target_level, 2)
    out["level_label"] = label

    # Entry window, from the config.
    before = clock.parse_hhmm(cfg["no_entries_before"]) if cfg.get("no_entries_before") else None
    after = clock.parse_hhmm(cfg["no_entries_after"]) if cfg.get("no_entries_after") else None
    out["window"] = {"from": cfg.get("no_entries_before"), "to": cfg.get("no_entries_after")}

    start_index = rng["bars"] if rng else 0
    trigger = None
    for index, bar in enumerate(bars[start_index:], start=start_index):
        if bar["c"] <= target_level:
            continue
        if min_volume_x and rng and rng["avg_volume"]:
            if bar["v"] / rng["avg_volume"] < min_volume_x:
                continue
        trigger = (index, bar)
        break

    if trigger is None:
        gap = target_level - bars[-1]["c"]
        out["status"] = STATUS_WAITING
        out["distance"] = round(gap, 2)
        out["distance_pct"] = round(gap / target_level * 100, 2) if target_level else 0
        out["message"] = ("WAITING. Last close %.2f (%s bar), %.2f under %s %.2f (%+.2f%%)."
                          % (bars[-1]["c"], bars[-1]["end"], gap, label, target_level,
                             -out["distance_pct"]))
        if after and now.time() > after:
            out["status"] = STATUS_EXPIRED
            out["message"] = ("EXPIRED. No close above %s %.2f before the %s entry cutoff."
                              % (label, target_level, cfg["no_entries_after"]))
    else:
        index, bar = trigger
        out["status"] = STATUS_TRIGGERED
        out["entry"] = bar["c"]
        out["entry_time"] = bar["end"]
        out["entry_bar"] = index + 1
        out["over_pct"] = round((bar["c"] - target_level) / target_level * 100, 2)
        out["volume_x"] = (round(bar["v"] / rng["avg_volume"], 2)
                           if rng and rng["avg_volume"] else None)
        bars_ago = len(bars) - 1 - index
        out["bars_ago"] = bars_ago
        out["stale"] = bars_ago > 1
        out["message"] = ("TRIGGERED at %s, %.2f: bar %d closed %.2f%% above %s %.2f. "
                          "Nothing was placed."
                          % (bar["end"], bar["c"], index + 1, out["over_pct"], label, target_level))
        trigger_time = clock.parse_hhmm(bar["end"])
        notes = []
        if before and trigger_time < before:
            notes.append("This fired before your %s entry window opens." % cfg["no_entries_before"])
        if after and trigger_time > after:
            notes.append("This fired after your %s entry cutoff." % cfg["no_entries_after"])
            out["status"] = STATUS_EXPIRED
        if bars_ago > 1:
            notes.append("The trigger is %d bars old; the last close is %.2f. A late entry is a "
                         "different trade." % (bars_ago, bars[-1]["c"]))
        out["notes"] = notes

    if write and out["status"] == STATUS_TRIGGERED:
        store.write("entry.json", out)
    return out


def watchlist_check(or_minutes: int | None = None) -> dict:
    """Run `check` on every symbol the scanner last wrote."""
    saved = store.read("watchlist.json") or {}
    rows = []
    for hit in (saved.get("hits") or [])[:25]:
        try:
            rows.append(check(hit["symbol"], or_minutes=or_minutes))
        except Exception as exc:  # noqa: BLE001
            rows.append({"symbol": hit["symbol"], "status": STATUS_NO_DATA, "message": str(exc)})
    return {"as_of": clock.fmt_dt(clock.now_ist()), "source": saved.get("mode"), "rows": rows}
