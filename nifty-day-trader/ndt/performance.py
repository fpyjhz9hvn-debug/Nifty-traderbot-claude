"""Expectancy, Kelly and behaviour tags on the paper account's own record.

Ported from `position-sizer` (Kelly criterion, the 1 % rule, the asymmetry of
losses) and `trade-performance-coach` / `signal-postmortem` (classify each
closed trade, tag the behaviour, feed it back) in
github.com/tradermonty/claude-trading-skills.

The one number that decides whether a strategy makes money is expectancy:

    expectancy_R = win_rate x avg_win_R - (1 - win_rate) x avg_loss_R

Positive and the edge is real so far; negative and no amount of sizing saves it.
Sizing decides how fast an edge compounds and how badly its absence hurts, which
is why the half-Kelly ceiling here is a CAP on the configured risk, never a
licence to raise it.

Everything is computed from closed round trips in trader/portfolio.json, net of
the estimated charges. With a handful of trades none of it means much, and the
output says so rather than pretending otherwise.
"""
from __future__ import annotations

import math

from . import clock, store

MIN_TRADES_FOR_KELLY = 20
MEANINGFUL_SAMPLE = 30


def _round_trips(portfolio: dict) -> list:
    """Pair each buy with the sell that closed it, FIFO."""
    lots: dict = {}
    closed = []
    for trade in portfolio.get("trades") or []:
        symbol = trade["symbol"]
        if trade["side"] == "buy":
            lots.setdefault(symbol, []).append(trade)
            continue
        entry = (lots.get(symbol) or [None]).pop(0) if lots.get(symbol) else None
        if not entry:
            continue
        qty = trade["qty"]
        gross = (trade["price"] - entry["price"]) * qty
        charges = float(entry.get("charges") or 0) + float(trade.get("charges") or 0)
        closed.append({
            "symbol": symbol,
            "display": trade.get("display", symbol),
            "qty": qty,
            "entry": entry["price"],
            "exit": trade["price"],
            "entry_at": entry["at"],
            "exit_at": trade["at"],
            "reason": trade.get("reason", ""),
            "gross": round(gross, 2),
            "charges": round(charges, 2),
            "net": round(gross - charges, 2),
            "planned_stop": (entry.get("plan") or {}).get("stop"),
            "planned_risk": (entry.get("plan") or {}).get("max_loss"),
        })
    return closed


def _classify(trade: dict) -> str:
    reason = (trade.get("reason") or "").lower()
    if "stop-loss" in reason:
        return "stop"
    if "take-profit" in reason:
        return "target"
    if "flat by" in reason:
        return "timed out"
    return "closed by hand"


def _behaviour_tags(trades: list, cfg: dict) -> list:
    """The patterns trade-performance-coach looks for, on this ledger."""
    tags = []
    max_loss = float(cfg["max_loss_per_trade"])
    size_creep = [t for t in trades if t.get("planned_risk") and
                  float(t["planned_risk"]) > max_loss + 0.01]
    if size_creep:
        tags.append({"tag": "size creep", "count": len(size_creep),
                     "detail": "%d trade(s) were sized past the %s max loss per trade."
                               % (len(size_creep), store.rupees(max_loss))})
    hand = [t for t in trades if _classify(t) == "closed by hand"]
    early = [t for t in hand if t["net"] < 0]
    if early:
        tags.append({"tag": "cut by hand at a loss", "count": len(early),
                     "detail": "%d trade(s) were closed by hand while losing, before the stop "
                               "was reached. That is a different trade from the one planned."
                               % len(early)})
    revenge = []
    window = float(cfg.get("revenge_window_minutes", 30))
    for i in range(1, len(trades)):
        if trades[i - 1]["net"] >= 0:
            continue
        try:
            gap = (clock.parse_ist(trades[i]["entry_at"])
                   - clock.parse_ist(trades[i - 1]["exit_at"])).total_seconds() / 60
        except ValueError:
            continue
        if 0 <= gap < window:
            revenge.append(trades[i])
    if revenge:
        tags.append({"tag": "revenge entry", "count": len(revenge),
                     "detail": "%d entry(ies) were opened within %g minutes of a loss."
                               % (len(revenge), window)})
    timed = [t for t in trades if _classify(t) == "timed out"]
    if timed and len(timed) / max(len(trades), 1) > 0.5:
        tags.append({"tag": "mostly times out", "count": len(timed),
                     "detail": "%d of %d trades reached neither the stop nor the target and were "
                               "closed by the clock. A target that is never reached is too far "
                               "away, or the entry is too late." % (len(timed), len(trades))})
    return tags


def _max_drawdown(curve: list) -> dict:
    peak, worst, worst_at = None, 0.0, None
    for point in curve:
        value = float(point["equity"])
        peak = value if peak is None else max(peak, value)
        fall = peak - value
        if fall > worst:
            worst, worst_at = fall, point["t"]
    return {"max_drawdown": round(worst, 2),
            "max_drawdown_pct": round(worst / peak * 100, 2) if peak else 0.0,
            "at": worst_at}


def report(write: bool = True) -> dict:
    cfg = store.config()
    portfolio = store.read("portfolio.json") or {}
    trades = _round_trips(portfolio)
    opening = float(portfolio.get("opening_balance") or cfg["budget"])

    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] < 0]
    scratches = [t for t in trades if t["net"] == 0]
    count = len(trades)

    avg_win = sum(t["net"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(t["net"] for t in losses) / len(losses)) if losses else 0.0
    win_rate = len(wins) / count if count else 0.0
    payoff = (avg_win / avg_loss) if avg_loss else None
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss) if count else 0.0
    expectancy_r = (expectancy / avg_loss) if avg_loss else None
    gross_win = sum(t["net"] for t in wins)
    gross_loss = abs(sum(t["net"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss else None

    kelly = half_kelly = None
    if payoff and count >= MIN_TRADES_FOR_KELLY:
        kelly = win_rate - (1 - win_rate) / payoff
        kelly = max(0.0, kelly)
        half_kelly = kelly / 2

    by_exit: dict = {}
    for trade in trades:
        kind = _classify(trade)
        row = by_exit.setdefault(kind, {"count": 0, "net": 0.0})
        row["count"] += 1
        row["net"] = round(row["net"] + trade["net"], 2)

    charges = sum(t["charges"] for t in trades)
    net = sum(t["net"] for t in trades)
    drawdown = _max_drawdown(portfolio.get("equity_curve") or [])

    verdict, caveats = _verdict(count, expectancy, expectancy_r, profit_factor, net, charges)

    out = {
        "as_of": clock.fmt_dt(clock.now_ist()),
        "round_trips": count,
        "wins": len(wins), "losses": len(losses), "scratches": len(scratches),
        "win_rate_pct": round(win_rate * 100, 1) if count else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "payoff_ratio": round(payoff, 2) if payoff else None,
        "expectancy": round(expectancy, 2),
        "expectancy_r": round(expectancy_r, 3) if expectancy_r is not None else None,
        "profit_factor": round(profit_factor, 2) if profit_factor else None,
        "gross_profit": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "charges": round(charges, 2),
        "charges_share_pct": round(charges / gross_win * 100, 1) if gross_win else None,
        "net": round(net, 2),
        "net_pct_of_opening": round(net / opening * 100, 2) if opening else None,
        "by_exit": by_exit,
        "drawdown": drawdown,
        "kelly_pct": round(kelly * 100, 1) if kelly is not None else None,
        "half_kelly_pct": round(half_kelly * 100, 1) if half_kelly is not None else None,
        "kelly_ready": count >= MIN_TRADES_FOR_KELLY,
        "sample_is_meaningful": count >= MEANINGFUL_SAMPLE,
        "behaviour": _behaviour_tags(trades, {**cfg, "revenge_window_minutes":
                                              cfg.get("revenge_window_minutes", 30)}),
        "trades": list(reversed(trades))[:50],
        "verdict": verdict,
        "caveats": caveats,
        "note": ("Expectancy is the number that decides whether a strategy makes money: "
                 "win rate x average win minus loss rate x average loss, net of charges. "
                 "Sizing changes how fast an edge compounds, never whether there is one."),
    }
    if write:
        store.write("performance.json", out)
    return out


def _verdict(count, expectancy, expectancy_r, profit_factor, net, charges):
    caveats = []
    if count == 0:
        return ("No closed trades yet. Nothing can be said about the edge until there are some.",
                ["With zero trades every statistic below is undefined, not zero."])
    if count < MIN_TRADES_FOR_KELLY:
        caveats.append("Only %d closed round trip(s). Under %d trades this is noise, not a "
                       "track record: a run of luck either way swamps the signal."
                       % (count, MIN_TRADES_FOR_KELLY))
    elif count < MEANINGFUL_SAMPLE:
        caveats.append("%d closed round trips. Treat every number here as provisional; %d+ is "
                       "where it starts to mean something." % (count, MEANINGFUL_SAMPLE))
    if charges and net and charges > abs(net):
        caveats.append("Charges (%s) are larger than the net result (%s). The cost of trading "
                       "is the dominant term at this size."
                       % (store.rupees(charges), store.rupees(net)))
    if expectancy > 0:
        head = ("Positive expectancy so far: %s a trade (%s per unit of risk). That is what the "
                "record shows, not a forecast." % (store.rupees(expectancy),
                                                   ("%+.3fR" % expectancy_r) if expectancy_r
                                                   else "n/a"))
    elif expectancy < 0:
        head = ("Negative expectancy: %s a trade (%s per unit of risk). On this record the "
                "strategy loses money, and sizing it up would only lose faster."
                % (store.rupees(expectancy),
                   ("%+.3fR" % expectancy_r) if expectancy_r else "n/a"))
    else:
        head = "Expectancy is flat."
    return head, caveats


def kelly_shares_cap(entry: float, stop: float) -> dict | None:
    """A half-Kelly ceiling on size, only once there is a record to base it on."""
    perf = report(write=False)
    if not perf["kelly_ready"] or perf["half_kelly_pct"] is None:
        return None
    risk = entry - stop
    if risk <= 0:
        return None
    budget = float(store.config()["budget"]) * perf["half_kelly_pct"] / 100
    return {
        "shares": int(math.floor(budget / risk)),
        "half_kelly_pct": perf["half_kelly_pct"],
        "budget": round(budget, 2),
        "based_on": "%d closed trades, win rate %.1f%%, payoff %.2f"
                    % (perf["round_trips"], perf["win_rate_pct"] or 0, perf["payoff_ratio"] or 0),
    }
