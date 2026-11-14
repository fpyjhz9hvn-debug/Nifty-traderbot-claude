"""Test the whole bank of setups, honestly.

The method is the one `docs/dev/strategy-research-bias-checklist.md` in
tradermonty/claude-trading-skills insists on, and it exists because testing many
ideas on one window is how a backtest turns into a story about the past:

1. **Pre-declare the bank.** All seven setups in ndt/setups.py are written down
   before anything is run. None is added afterwards because it looked good.
2. **Split the window.** The older 60 % of sessions are the DESIGN half, the
   newer 40 % the CONFIRM half. Only results that hold in BOTH halves count for
   anything; a setup that is positive in the design half alone has told you
   nothing except that it fits the past.
3. **Report every setup**, winners and losers, with its trade count.
4. **Count the comparisons.** Testing 7 setups x 2 gate states means 14 tries,
   so the best one looks good partly because it is the best of 14. The report
   says so, and names the number.
5. **Charge it.** Every trade pays the same estimated NSE intraday charges the
   paper broker applies. A gross edge smaller than the cost of trading is not an
   edge.

What this still cannot do: prove an edge. 60 days is a small window, the fills
are optimistic, and surviving one split is weak evidence, not strong. The honest
ceiling on a result here is "worth paper-trading forward", never "trade this".
"""
from __future__ import annotations

import math

from . import backtest, broker, clock, data, regime, setups, store, universe

DESIGN_SHARE = 0.6


def _load(group: str):
    """{symbol: {date: bars}} plus each session's previous close."""
    loaded, failed = {}, []
    for symbol in universe.symbols(group):
        try:
            sessions = backtest._sessions(symbol)
        except data.DataError:
            failed.append(universe.display(symbol))
            continue
        if sessions:
            loaded[symbol] = sessions
    return loaded, failed


def _summarise(trades: list, label: str) -> dict:
    if not trades:
        return {"label": label, "trades": 0, "expectancy_r": None, "net": 0.0,
                "win_rate_pct": None, "total_r": 0.0}
    wins = [t for t in trades if t["r"] > 0]
    losses = [t for t in trades if t["r"] < 0]
    total_r = sum(t["r"] for t in trades)
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
        "sessions": len({t["date"] for t in trades}),
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "avg_win_r": round(sum(t["r"] for t in wins) / len(wins), 3) if wins else 0.0,
        "avg_loss_r": round(abs(sum(t["r"] for t in losses) / len(losses)), 3) if losses else 0.0,
        "expectancy_r": round(total_r / len(trades), 4),
        "total_r": round(total_r, 2),
        "max_drawdown_r": round(worst, 2),
        "by_outcome": by_outcome,
        "net": round(sum(t["net"] for t in trades), 2),
        "charges": round(sum(t["charges"] for t in trades), 2),
        "gross": round(sum(t["gross"] for t in trades), 2),
    }


def run(group: str = "nifty50", target_r: float | None = None, write: bool = True) -> dict:
    cfg = store.config()
    target_r = float(target_r or cfg.get("default_target_r", 2.0))
    or_minutes = int(cfg.get("or_minutes", 15))
    max_per_day = int(cfg["max_trades_per_day"])
    risk_budget = float(cfg["max_loss_per_trade"])
    first_slot, last_slot = setups.window_slots(cfg)

    loaded, failed = _load(group)
    all_days = sorted({day for sessions in loaded.values() for day in sessions})
    if len(all_days) < 20:
        raise ValueError("Only %d sessions available: too few to split." % len(all_days))
    split_at = int(len(all_days) * DESIGN_SHARE)
    design_days = set(all_days[:split_at])
    confirm_days = set(all_days[split_at:])

    regimes = backtest._regime_by_day()

    # Every setup, every symbol, every session -- collected first, capped later.
    raw: dict = {s["key"]: {} for s in setups.BANK}
    for symbol, sessions in loaded.items():
        days = sorted(sessions)
        for position, day in enumerate(days):
            bars = sessions[day]
            prev_close = sessions[days[position - 1]][-1]["c"] if position else None
            ctx = setups.context(bars, or_minutes, prev_close, first_slot, last_slot)
            for setup in setups.BANK:
                trade = setups.simulate(bars, setup, ctx, target_r)
                if not trade:
                    continue
                trade.update({"symbol": symbol, "display": universe.display(symbol), "date": day})
                raw[setup["key"]].setdefault(day, []).append(trade)

    results = []
    for setup in setups.BANK:
        per_day = raw[setup["key"]]
        taken = []
        for day in sorted(per_day):
            rows = sorted(per_day[day], key=lambda t: (t["entry_index"], t["display"]))[:max_per_day]
            allowed = regimes.get(day, {}).get("allows")
            for trade in rows:
                qty = backtest.size_like_the_app(trade["entry"], trade["risk_per_share"], cfg)
                if qty < 1:
                    continue
                gross = (trade["exit"] - trade["entry"]) * qty
                charges = (broker.charges("buy", qty, trade["entry"], cfg)["total"]
                           + broker.charges("sell", qty, trade["exit"], cfg)["total"])
                taken.append({**trade, "qty": qty, "gross": round(gross, 2),
                              "charges": round(charges, 2), "net": round(gross - charges, 2),
                              "regime_allowed": bool(allowed),
                              "half": "design" if day in design_days else "confirm"})
        gated = [t for t in taken if t["regime_allowed"]]
        row = {
            "key": setup["key"], "name": setup["name"], "family": setup["family"],
            "idea": setup["idea"],
            "all": _summarise(taken, "every session"),
            "design": _summarise([t for t in taken if t["half"] == "design"], "design half"),
            "confirm": _summarise([t for t in taken if t["half"] == "confirm"], "confirm half"),
            "gated_all": _summarise(gated, "regime gate on"),
            "gated_design": _summarise([t for t in gated if t["half"] == "design"], "gated design"),
            "gated_confirm": _summarise([t for t in gated if t["half"] == "confirm"], "gated confirm"),
        }
        row["held_up"] = _held_up(row)
        results.append(row)

    results.sort(key=lambda r: (r["all"]["expectancy_r"] is not None,
                                r["all"]["expectancy_r"] or -99), reverse=True)
    comparisons = len(results) * 2
    survivors = [r for r in results if r["held_up"]["survives"]]

    out = {
        "as_of": clock.fmt_dt(clock.now_ist()),
        "universe": group,
        "symbols": len(loaded),
        "failed": failed,
        "target_r": target_r,
        "sessions": len(all_days),
        "from": all_days[0], "to": all_days[-1],
        "split": {
            "design_sessions": len(design_days), "confirm_sessions": len(confirm_days),
            "design_to": all_days[split_at - 1], "confirm_from": all_days[split_at],
            "share": DESIGN_SHARE,
        },
        "comparisons": comparisons,
        "results": results,
        "survivors": [r["key"] for r in survivors],
        "verdict": _verdict(results, survivors, comparisons),
        "method": [
            "All %d setups were written down before anything was run; none was added after "
            "seeing a result." % len(setups.BANK),
            "The older %.0f%% of sessions are the design half, the newer %.0f%% the confirm "
            "half. A setup counts only if it is positive in BOTH."
            % (DESIGN_SHARE * 100, (1 - DESIGN_SHARE) * 100),
            "Every setup is reported, including the ones that lose.",
            "%d comparisons were made (%d setups x gate on/off), so the best result is partly "
            "the best of %d tries." % (comparisons, len(results), comparisons),
            "Every trade pays the same estimated NSE intraday charges the paper broker applies.",
        ],
        "limits": [
            "60 days is a small window and these are optimistic fills: entry at the bar close, "
            "stop and target AT the level, no slippage or partial fills.",
            "The universe is today's NIFTY list applied to a past window (survivorship).",
            "Surviving one split is weak evidence. The honest ceiling on anything here is "
            "'worth paper-trading forward', never 'trade this'.",
        ],
    }
    if write:
        store.write("research.json", out)
    return out


def _held_up(row: dict) -> dict:
    """Does it work in both halves, on enough trades, net of charges?"""
    checks = []
    for label, arm in (("every session", row["all"]), ("regime gate on", row["gated_all"])):
        design = row["design"] if label == "every session" else row["gated_design"]
        confirm = row["confirm"] if label == "every session" else row["gated_confirm"]
        enough = (design["trades"] or 0) >= 20 and (confirm["trades"] or 0) >= 20
        both_positive = ((design["expectancy_r"] or -1) > 0 and (confirm["expectancy_r"] or -1) > 0)
        net_positive = (arm["net"] or 0) > 0
        checks.append({
            "arm": label,
            "design_expectancy_r": design["expectancy_r"],
            "confirm_expectancy_r": confirm["expectancy_r"],
            "design_trades": design["trades"], "confirm_trades": confirm["trades"],
            "both_halves_positive": both_positive,
            "enough_trades": enough,
            "net_positive_after_charges": net_positive,
            "survives": bool(both_positive and enough and net_positive),
        })
    survives = any(c["survives"] for c in checks)
    return {"survives": survives, "arms": checks}


def _verdict(results, survivors, comparisons) -> str:
    if not results:
        return "Nothing ran."
    best = results[0]
    head = ("%d setups tested over the same sessions, %d comparisons in all. Best by raw "
            "expectancy: %s at %s a trade over %d trades."
            % (len(results), comparisons, best["name"],
               ("%+.4fR" % best["all"]["expectancy_r"]) if best["all"]["expectancy_r"] is not None
               else "n/a", best["all"]["trades"]))
    if not survivors:
        return (head + " None of them survived the split: not one was positive in both the "
                "design and the confirm half on 20+ trades each, net of charges. On this window "
                "there is no setup in the bank with a demonstrable edge, and the right "
                "conclusion is to keep paper-trading rather than to pick the top row.")
    names = ", ".join(r["name"] for r in results if r["held_up"]["survives"])
    return (head + " %d survived the split (positive in both halves, 20+ trades each, net of "
            "charges): %s. That is a reason to paper-trade it forward and watch, not a reason "
            "to believe it: with %d comparisons, one survivor is roughly what chance alone "
            "would produce." % (len(survivors), names, comparisons))
