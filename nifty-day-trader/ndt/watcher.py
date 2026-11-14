"""The session watcher: hunt the surviving setup, all session, and propose.

It scans the universe for the opening-range low reclaim -- the one setup in
ndt/setups.py that survived the design / confirm split in ndt/research.py --
checks every gate the Risk Manager would check, and writes what it finds to
trader/candidates.json for the UI to show.

**It never places an order, and it never can.** The broker skill is explicit
about why: an order needs a human to say yes for that exact order, and nobody is
watching a background loop at 11:40 on a Tuesday. So this produces a shortlist
with the arithmetic already done, and you still walk it through the trade desk.

Two stages, like the opening-range scanner, so a full pass costs a few requests
rather than one per name:

1. Spark closes for every symbol. The reclaim is defined on CLOSES, and the
   lowest close of the opening range is never below its true low, so anything
   that lost the real low also lost this proxy: a superset, nothing missed.
2. The real chart, with highs and lows, for each candidate only.
"""
from __future__ import annotations

import math

from . import (backtest, broker, clock, data, discipline, entry as entry_mod,
               regime as regime_mod, scanner, store, universe)

SETUP = "or_low_reclaim"
SETUP_NAME = "opening-range low reclaim"


def gates(as_of=None) -> dict:
    """Everything that would stop an entry, checked once for the whole pass."""
    cfg = store.config()
    breaker = discipline.circuit_breaker(as_of)
    heat = discipline.portfolio_heat()
    revenge = discipline.revenge_check(as_of)
    regime_state = regime_mod.cached(max_age_minutes=180)
    now = as_of or clock.now_ist()

    window_open = True
    window_note = None
    if not clock.is_open(now, store.holidays()):
        window_open, window_note = False, "the session is not running (09:15 to 15:30 IST)"
    else:
        before = cfg.get("no_entries_before")
        after = cfg.get("no_entries_after")
        if before and now.time() < clock.parse_hhmm(before):
            window_open, window_note = False, "no new entries before %s IST" % before
        elif after and now.time() > clock.parse_hhmm(after):
            window_open, window_note = False, "no new entries after %s IST" % after

    log = store.read("state/broker.json") or {}
    today = now.strftime("%Y-%m-%d")
    entries_today = [o for o in (log.get("orders") or [])
                     if o.get("date") == today and o.get("status") == "placed"
                     and o.get("kind") == "entry"]
    trades_left = int(cfg["max_trades_per_day"]) - len(entries_today)

    blocking = []
    if not window_open:
        blocking.append("window: %s" % window_note)
    if not breaker["allows_new_entries"]:
        blocking.append("circuit breaker: %s" % breaker["rationale"])
    if heat["over"]:
        blocking.append("portfolio heat: %.1f%% is over the %g%% cap"
                        % (heat["heat_pct"], heat["limit_pct"]))
    if revenge.get("revenge_window"):
        blocking.append("revenge window: %s" % revenge["detail"])
    if regime_state is None:
        blocking.append("market regime: not read yet today")
    elif not regime_state["allows_new_entries"]:
        blocking.append("market regime: %s, %s" % (regime_state["regime"],
                                                   regime_state["recommendation"]))
    if trades_left <= 0:
        blocking.append("trades: %d entries already placed today, your limit is %d"
                        % (len(entries_today), cfg["max_trades_per_day"]))

    return {
        "clear": not blocking,
        "blocking": blocking,
        "window_open": window_open,
        "trades_left": max(trades_left, 0),
        "circuit_breaker": breaker["recommendation"],
        "heat_pct": heat["heat_pct"],
        "regime": regime_state and regime_state["regime"],
        "regime_recommendation": regime_state and regime_state["recommendation"],
        "regime_allows": regime_state and regime_state["allows_new_entries"],
    }


def _stage_one(symbols: list, or_minutes: int) -> list:
    """Names whose closes lost the opening-range floor and came back above it."""
    spark = data.intraday_spark(symbols, "5m", ttl=50.0)
    count = max(1, or_minutes // clock.BAR_MINUTES)
    out = []
    for symbol in symbols:
        row = spark.get(symbol)
        if not row or len(row["bars"]) <= count + 1:
            continue
        bars = row["bars"]
        if bars[0]["start"] != clock.SESSION_OPEN.strftime("%H:%M"):
            continue
        floor_proxy = min(b["c"] for b in bars[:count])
        broke = False
        for bar in bars[count:]:
            if bar["c"] < floor_proxy:
                broke = True
            elif broke and bar["c"] > floor_proxy:
                out.append((symbol, (row.get("price") or 0) * (row.get("volume") or 0)))
                break
    out.sort(key=lambda c: c[1], reverse=True)
    return [symbol for symbol, _ in out]


def scan(group: str | None = None, force: bool = False, write: bool = True,
         max_confirm: int = 25) -> dict:
    """One pass. Returns the shortlist, with the sizing already worked out."""
    cfg = store.config()
    group = group or cfg.get("universe_group", "nifty50")
    or_minutes = int(cfg.get("or_minutes", 15))
    now = clock.now_ist()
    gate_state = gates(now)

    out = {
        "as_of": clock.fmt_dt(now),
        "setup": SETUP,
        "setup_name": SETUP_NAME,
        "universe": group,
        "gates": gate_state,
        "candidates": [],
        "scanned": 0,
        "checked": 0,
        "note": ("A shortlist, not an instruction. Nothing is placed here: take one to the "
                 "trade desk, where the risk check and your yes still stand between it and a "
                 "paper fill."),
    }

    if not gate_state["window_open"] and not force:
        out["message"] = "Not scanning: %s." % (gate_state["blocking"][0] if gate_state["blocking"]
                                                else "outside the entry window")
        if write:
            store.write("candidates.json", out)
        return out

    symbols = universe.symbols(group)
    out["scanned"] = len(symbols)
    try:
        shortlist = _stage_one(symbols, or_minutes)
    except data.DataError as exc:
        out["message"] = "Stage 1 failed: %s" % exc
        if write:
            store.write("candidates.json", out)
        return out

    held = set((store.read("portfolio.json") or {}).get("positions") or {})
    blocked = {str(b).upper() for b in (cfg.get("blocked") or [])}
    candidates = []
    for symbol in shortlist[:max_confirm]:
        if symbol in held or universe.display(symbol) in blocked:
            continue
        out["checked"] += 1
        try:
            signal = entry_mod.check(symbol, setup=SETUP, or_minutes=or_minutes, write=False)
        except Exception:  # noqa: BLE001
            continue
        if signal.get("status") != entry_mod.STATUS_TRIGGERED:
            continue
        entry = float(signal["entry"])
        stop = float(signal["suggested_stop"])
        risk = round(entry - stop, 2)
        if risk <= 0 or risk / entry < 0.0005:
            continue
        target_r = float(cfg.get("default_target_r", 2.0))
        target = math.floor((entry + target_r * risk) * 100 + 0.5) / 100
        qty = backtest.size_like_the_app(entry, risk, cfg)
        if qty < 1:
            continue
        value = round(qty * entry, 2)
        max_loss = round(qty * risk, 2)
        est_charges = (broker.charges("buy", qty, entry, cfg)["total"]
                       + broker.charges("sell", qty, target, cfg)["total"])
        try:
            last = data.quote(symbol, ttl=45.0)["price"]
        except data.DataError:
            last = entry
        candidates.append({
            "symbol": symbol,
            "display": signal["display"],
            "name": signal["name"],
            "entry": entry,
            "entry_time": signal["entry_time"],
            "entry_bar": signal["entry_bar"],
            "broke_at": signal.get("broke_at"),
            "or_low": (signal.get("opening_range") or {}).get("low"),
            "or_high": (signal.get("opening_range") or {}).get("high"),
            "stop": stop,
            "target": target,
            "target_r": target_r,
            "risk_per_share": risk,
            "qty": qty,
            "value": value,
            "max_loss": max_loss,
            "est_charges": round(est_charges, 2),
            "last": last,
            "drift_pct": round((last - entry) / entry * 100, 2),
            "bars_ago": signal.get("bars_ago"),
            "stale": bool(signal.get("stale")),
            "still_valid": last > stop and last < target,
            "why": signal.get("message"),
        })

    candidates.sort(key=lambda c: (c["stale"], -c["max_loss"]))
    out["candidates"] = candidates
    fresh = [c for c in candidates if not c["stale"] and c["still_valid"]]
    out["fresh"] = len(fresh)
    if not gate_state["clear"]:
        out["message"] = ("%d reclaim(s) found, but no entry is allowed right now: %s."
                          % (len(candidates), "; ".join(gate_state["blocking"])))
    elif not candidates:
        out["message"] = "No name has lost and reclaimed its opening-range low yet."
    else:
        out["message"] = ("%d reclaim(s), %d of them fresh and still between the stop and the "
                          "target. %d trade(s) left today."
                          % (len(candidates), len(fresh), gate_state["trades_left"]))
    if write:
        store.write("candidates.json", out)
    return out


def saved() -> dict:
    return store.read("candidates.json") or {"candidates": [], "message": "Not run yet."}
