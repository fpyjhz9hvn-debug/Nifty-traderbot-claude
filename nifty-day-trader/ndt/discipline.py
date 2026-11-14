"""Discipline: the circuit breaker, portfolio heat and the pre-trade gate.

Ported from github.com/tradermonty/claude-trading-skills:

* `drawdown-circuit-breaker` -- stop taking new risk after realised damage.
  Daily, weekly and monthly loss limits, plus a cooldown after consecutive
  losing trades. Realised P&L only: an open position that is merely underwater
  has not cost anything yet.
* `pre-trade-discipline-gate` -- before an order is placed: is there a written
  plan, a predefined stop, a size inside the plan, and is the actual risk no
  bigger than the planned risk? Is the market regime allowing new entries? Is
  the circuit breaker clear? A revenge trade right after a loss is blocked.
* `position-sizer` -- portfolio heat: total open risk across all positions,
  capped at a few percent of the account.

All of it reads this app's own paper ledger. Day, week and month boundaries are
IST, because the session is.

What this cannot do: make a losing strategy win. It limits how much a bad run
costs, which is a different and more achievable thing.
"""
from __future__ import annotations

import datetime as _dt

from . import clock, store

# Defaults, in percent of the account. The skill's numbers are for swing
# trading; the intraday ones here are tighter because an intraday system takes
# far more trades in a day.
DEFAULTS = {
    "max_daily_loss_pct": 2.0,
    "max_weekly_loss_pct": 5.0,
    "max_monthly_loss_pct": 8.0,
    "losing_streak_n": 2,
    "cooldown_minutes": 45,
    "max_portfolio_heat_pct": 6.0,
    "revenge_window_minutes": 30,
}


def settings() -> dict:
    cfg = store.config()
    out = dict(DEFAULTS)
    for key in out:
        if cfg.get(key) is not None:
            out[key] = cfg[key]
    return out


def _closed_trades(portfolio: dict) -> list:
    """Every realised exit, newest last, with its P&L net of charges."""
    rows = []
    for trade in portfolio.get("trades") or []:
        if trade.get("side") != "sell" or trade.get("realised") is None:
            continue
        try:
            when = clock.parse_ist(trade["at"])
        except (ValueError, KeyError):
            continue
        rows.append({"at": when, "at_text": trade["at"], "symbol": trade.get("symbol"),
                     "display": trade.get("display"), "realised": float(trade["realised"]),
                     "reason": trade.get("reason", "")})
    rows.sort(key=lambda r: r["at"])
    return rows


def circuit_breaker(as_of: _dt.datetime | None = None) -> dict:
    """TRADING_ALLOWED, COOLDOWN or HALTED, from realised paper P&L."""
    cfg = settings()
    account_size = float(store.config()["budget"])
    portfolio = store.read("portfolio.json") or {}
    opening = float(portfolio.get("opening_balance") or account_size)
    now = (as_of or clock.now_ist()).astimezone(clock.IST)
    trades = _closed_trades(portfolio)

    today = now.date()
    week_start = today - _dt.timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    pnl_today = sum(t["realised"] for t in trades if t["at"].date() == today)
    pnl_week = sum(t["realised"] for t in trades if t["at"].date() >= week_start)
    pnl_month = sum(t["realised"] for t in trades if t["at"].date() >= month_start)

    streak, last_loss_at = 0, None
    for trade in reversed(trades):
        if trade["realised"] < 0:
            streak += 1
            if last_loss_at is None:
                last_loss_at = trade["at"]
        else:
            break

    limits = {
        "daily": opening * cfg["max_daily_loss_pct"] / 100,
        "weekly": opening * cfg["max_weekly_loss_pct"] / 100,
        "monthly": opening * cfg["max_monthly_loss_pct"] / 100,
    }
    triggered = []
    if -pnl_today >= limits["daily"]:
        triggered.append({
            "rule": "max_daily_loss", "state": "HALTED",
            "threshold": round(limits["daily"], 2), "observed": round(-pnl_today, 2),
            "active_until": "the next trading session",
            "detail": "Today's realised loss %s is at or past your %.1f%% daily limit (%s)."
                      % (store.rupees(-pnl_today), cfg["max_daily_loss_pct"],
                         store.rupees(limits["daily"]))})
    if -pnl_week >= limits["weekly"]:
        triggered.append({
            "rule": "weekly_drawdown", "state": "HALTED",
            "threshold": round(limits["weekly"], 2), "observed": round(-pnl_week, 2),
            "active_until": "Monday",
            "detail": "This week's realised loss %s is at or past your %.1f%% weekly limit."
                      % (store.rupees(-pnl_week), cfg["max_weekly_loss_pct"])})
    if -pnl_month >= limits["monthly"]:
        triggered.append({
            "rule": "monthly_drawdown", "state": "HALTED",
            "threshold": round(limits["monthly"], 2), "observed": round(-pnl_month, 2),
            "active_until": "the first of next month",
            "detail": "This month's realised loss %s is at or past your %.1f%% monthly limit."
                      % (store.rupees(-pnl_month), cfg["max_monthly_loss_pct"])})

    cooldown_until = None
    if streak >= int(cfg["losing_streak_n"]) and last_loss_at:
        cooldown_until = last_loss_at + _dt.timedelta(minutes=float(cfg["cooldown_minutes"]))
        if now < cooldown_until:
            triggered.append({
                "rule": "losing_streak_cooldown", "state": "COOLDOWN",
                "threshold": int(cfg["losing_streak_n"]), "observed": streak,
                "active_until": clock.fmt_dt(cooldown_until),
                "detail": "%d losing exits in a row, the last at %s. Cooling off until %s."
                          % (streak, clock.fmt_time(last_loss_at), clock.fmt_time(cooldown_until))})

    if any(t["state"] == "HALTED" for t in triggered):
        recommendation = "HALTED"
    elif triggered:
        recommendation = "COOLDOWN"
    else:
        recommendation = "TRADING_ALLOWED"

    return {
        "as_of": clock.fmt_dt(now),
        "recommendation": recommendation,
        "allows_new_entries": recommendation == "TRADING_ALLOWED",
        "triggered_rules": triggered,
        "metrics": {
            "realised_today": round(pnl_today, 2),
            "realised_week": round(pnl_week, 2),
            "realised_month": round(pnl_month, 2),
            "consecutive_losses": streak,
            "last_loss_at": clock.fmt_dt(last_loss_at) if last_loss_at else None,
            "closed_trades": len(trades),
        },
        "limits": {k: round(v, 2) for k, v in limits.items()},
        "config": cfg,
        "rationale": (triggered[0]["detail"] if triggered else
                      "No loss limit is active. Today's realised P&L is %s of a %s daily limit."
                      % (store.rupees(pnl_today), store.rupees(limits["daily"]))),
        "note": ("Realised damage only: an open position that is underwater has not cost "
                 "anything yet. Getting out is never blocked by any of this."),
    }


def portfolio_heat() -> dict:
    """Total open risk across every position, as a share of the account."""
    cfg = settings()
    portfolio = store.read("portfolio.json") or {}
    opening = float(portfolio.get("opening_balance") or store.config()["budget"])
    rows, total = [], 0.0
    for symbol, pos in (portfolio.get("positions") or {}).items():
        stop = pos.get("stop")
        risk = (pos["avg_price"] - float(stop)) * pos["qty"] if stop else None
        if risk and risk > 0:
            total += risk
        rows.append({"symbol": symbol, "display": pos.get("display", symbol),
                     "qty": pos["qty"], "entry": pos["avg_price"], "stop": stop,
                     "open_risk": round(risk, 2) if risk else None,
                     "has_stop": bool(stop)})
    limit = opening * cfg["max_portfolio_heat_pct"] / 100
    return {
        "heat": round(total, 2),
        "heat_pct": round(total / opening * 100, 2) if opening else 0.0,
        "limit": round(limit, 2),
        "limit_pct": cfg["max_portfolio_heat_pct"],
        "room": round(max(limit - total, 0), 2),
        "over": total > limit,
        "positions": rows,
        "note": ("Open risk is what every open position would lose together if each one hit "
                 "its stop. Past the limit, no new entry is added until something is closed "
                 "or moved to breakeven."),
    }


def revenge_check(as_of: _dt.datetime | None = None) -> dict:
    """Is this entry landing inside the window right after a loss?"""
    cfg = settings()
    now = (as_of or clock.now_ist()).astimezone(clock.IST)
    trades = _closed_trades(store.read("portfolio.json") or {})
    losses = [t for t in trades if t["realised"] < 0]
    if not losses:
        return {"revenge_window": False}
    last = losses[-1]
    minutes = (now - last["at"]).total_seconds() / 60
    inside = 0 <= minutes < float(cfg["revenge_window_minutes"])
    return {
        "revenge_window": inside,
        "minutes_since_loss": round(minutes, 1),
        "window_minutes": cfg["revenge_window_minutes"],
        "last_loss": {"display": last["display"], "realised": last["realised"],
                      "at": last["at_text"], "reason": last["reason"]},
        "detail": ("The last loss (%s, %s) was %.0f minutes ago, inside your %g-minute "
                   "window. The trade right after a loss is the one most often taken for the "
                   "wrong reason." % (last["display"], store.rupees(last["realised"]), minutes,
                                      cfg["revenge_window_minutes"])) if inside else None,
    }


def gate(order: dict | None = None, plan: dict | None = None, as_of=None,
         use_cached_regime: bool = True) -> dict:
    """The pre-trade discipline gate: GO, REVIEW_REQUIRED or NO_GO."""
    from . import regime as regime_mod

    cfg = store.config()
    plan = plan if plan is not None else store.read("plan.json")
    checks, blocks, reviews = [], [], []

    def check(name, ok, detail, blocking=True):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            (blocks if blocking else reviews).append("%s: %s" % (name, detail))

    check("written plan", bool(plan),
          "a plan with an entry, a stop and a target exists" if plan
          else "no plan has been written for this trade")
    stop = (order or {}).get("stop_loss_stop_price") or (plan or {}).get("stop")
    check("stop predefined", bool(stop),
          "the stop is set at %s" % stop if stop else "no stop is set")

    if order and plan:
        planned_risk = float(cfg["max_loss_per_trade"])
        actual_risk = float((order.get("plan") or {}).get("max_loss") or 0)
        check("size within plan", actual_risk <= planned_risk + 0.01,
              "risk at the stop %s against a planned ceiling of %s"
              % (store.rupees(actual_risk), store.rupees(planned_risk)))

    breaker = circuit_breaker(as_of)
    check("circuit breaker", breaker["allows_new_entries"], breaker["rationale"])

    heat = portfolio_heat()
    check("portfolio heat", not heat["over"],
          "open risk %s of a %s limit (%.1f%% of the account)"
          % (store.rupees(heat["heat"]), store.rupees(heat["limit"]), heat["heat_pct"]))

    revenge = revenge_check(as_of)
    check("revenge window", not revenge["revenge_window"],
          revenge.get("detail") or "no loss inside the cooling-off window")

    regime_state = regime_mod.cached() if use_cached_regime else None
    if regime_state is None:
        try:
            regime_state = regime_mod.assess()
        except Exception as exc:  # noqa: BLE001
            regime_state = None
            check("market regime", False,
                  "the regime could not be read (%s), so it cannot be confirmed" % exc,
                  blocking=False)
    if regime_state:
        if regime_state.get("stale"):
            reviews.append("market regime: the last read is %.0f minutes old."
                           % regime_state.get("age_minutes", 0))
        check("market regime", regime_state["allows_new_entries"],
              "%s, %s -- %s" % (regime_state["regime"], regime_state["recommendation"],
                                regime_state["rationale"]))

    if blocks:
        decision = "NO_GO"
    elif reviews:
        decision = "REVIEW_REQUIRED"
    else:
        decision = "GO"

    return {
        "as_of": clock.fmt_dt(as_of or clock.now_ist()),
        "decision": decision,
        "checks": checks,
        "blocks": blocks,
        "reviews": reviews,
        "circuit_breaker": breaker,
        "heat": heat,
        "revenge": revenge,
        "regime": regime_state and {k: regime_state[k] for k in
                                    ("regime", "recommendation", "risk_level",
                                     "exposure_ceiling_pct", "allows_new_entries", "rationale")
                                    if k in regime_state},
        "note": ("A GO means nothing was violated. It is not a recommendation, and it does not "
                 "make the trade good."),
    }
