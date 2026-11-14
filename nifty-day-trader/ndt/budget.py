"""Budget Manager (trader-budget, ported to NSE).

Turns a plan ("buy RELIANCE at 1,340.50, stop 1,332.10") into an exact order
("buy 23 shares"), inside rules set once. The stop decides the size: the
further away the stop, the fewer shares.

A proposal only. The Risk Manager checks it; the paper broker asks for a yes.
A stop too far away for the max loss is a plain refusal, never a smaller order
at a wider stop.
"""
from __future__ import annotations

import math

from . import clock, discipline, performance, store, universe


class SizeRefused(Exception):
    pass


def size(plan: dict | None = None, limit_price: float | None = None, write: bool = True) -> dict:
    cfg = store.config()
    plan = plan or store.read("plan.json")
    if not plan:
        raise SizeRefused("No plan to size. Run the Exit Planner first.")

    symbol = universe.normalise(plan["symbol"])
    today = clock.now_ist().strftime("%Y-%m-%d")
    if plan.get("date") and plan["date"] != today:
        raise SizeRefused("That plan is from %s, not today. Make a new one." % plan["date"])
    if universe.display(symbol) in [str(b).upper() for b in (cfg.get("blocked") or [])]:
        raise SizeRefused("%s is on your blocked list." % universe.display(symbol))
    if plan.get("side", "long") != "long":
        raise SizeRefused("Long side only.")

    stop = float(plan["stop"])
    target = float(plan["target"])
    plan_entry = float(plan["entry"])

    # The entry it sizes at: a limit price, else the higher of the plan's entry
    # and a fresh quote. A price that ran up means a bigger risk at the same stop.
    entry, entry_why = plan_entry, "the plan's entry"
    prices = store.read("prices.json") or {}
    fresh = (prices.get("prices") or {}).get(symbol)
    fresh_age = None
    if fresh and prices.get("as_of"):
        try:
            age = (clock.now_ist() - clock.parse_ist(prices["as_of"])).total_seconds() / 60
            fresh_age = round(age, 1)
        except ValueError:
            fresh_age = None
    if limit_price:
        entry, entry_why = float(limit_price), "your limit price"
    elif fresh and fresh_age is not None and fresh_age < 15 and float(fresh) > plan_entry:
        entry, entry_why = float(fresh), "a fresh quote %.1f minutes old, above the plan's entry" % fresh_age

    if stop >= entry:
        raise SizeRefused("The stop %.2f is not below the entry %.2f." % (stop, entry))
    if target <= entry:
        raise SizeRefused("The target %.2f is not above the entry %.2f: the move it was waiting "
                          "for has already happened." % (target, entry))
    risk = round(entry - stop, 2)
    reward_risk = round((target - entry) / risk, 2)
    if reward_risk < float(cfg.get("min_reward_risk", 1.0)):
        raise SizeRefused("Reward:risk is %.2f at %.2f, under your minimum %.2f."
                          % (reward_risk, entry, cfg.get("min_reward_risk", 1.0)))

    portfolio = store.read("portfolio.json") or {}
    budget = float(cfg["budget"])
    cash = float(portfolio.get("cash", budget))
    buffer_amount = budget * float(cfg["cash_buffer_pct"]) / 100

    limits = [
        ("max loss per trade", math.floor(float(cfg["max_loss_per_trade"]) / risk),
         "%s / %.2f risk a share" % (store.rupees(cfg["max_loss_per_trade"]), risk)),
        ("position cap", math.floor(budget * float(cfg["max_position_pct"]) / 100 / entry),
         "%g%% of %s / %.2f" % (cfg["max_position_pct"], store.rupees(budget), entry)),
        ("max order value", math.floor(float(cfg["max_order_value"]) / entry),
         "%s / %.2f" % (store.rupees(cfg["max_order_value"]), entry)),
        ("cash after the buffer", math.floor(max(min(cash, budget) - buffer_amount, 0) / entry),
         "%s cash minus the %g%% buffer, / %.2f"
         % (store.rupees(cash), cfg["cash_buffer_pct"], entry)),
    ]

    # Portfolio heat: total open risk across every position, capped. From the
    # position-sizer skill -- past the cap, nothing new is added until something
    # is closed or moved to breakeven.
    heat = discipline.portfolio_heat()
    limits.append(("portfolio heat", math.floor(max(heat["room"], 0) / risk),
                   "%s of open-risk room left under the %g%% heat cap (%s)"
                   % (store.rupees(heat["room"]), heat["limit_pct"],
                      store.rupees(heat["limit"]))))

    # Half-Kelly ceiling, once the paper account has a record worth using.
    kelly = performance.kelly_shares_cap(entry, stop)
    if kelly:
        limits.append(("half-Kelly ceiling", kelly["shares"],
                       "half Kelly %.1f%% of %s (%s)"
                       % (kelly["half_kelly_pct"], store.rupees(budget), kelly["based_on"])))

    qty = min(l[1] for l in limits)
    decided_by = [l[0] for l in limits if l[1] == qty][0]

    if qty < 1:
        raise SizeRefused("Zero shares fit: one share risks %.2f, and the binding limit is the %s."
                          % (risk, decided_by))
    value = round(qty * entry, 2)
    if value < float(cfg["min_order_value"]):
        raise SizeRefused("%d share(s) is %s, under your %s minimum order."
                          % (qty, store.rupees(value), store.rupees(cfg["min_order_value"])))

    max_loss = round(qty * risk, 2)
    order = {
        "symbol": symbol,
        "display": universe.display(symbol),
        "name": universe.names().get(symbol, symbol),
        "side": "buy",
        "qty": qty,
        "type": "limit" if limit_price else "market",
        "limit_price": round(float(limit_price), 2) if limit_price else None,
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit_limit_price": round(target, 2),
        "stop_loss_stop_price": round(stop, 2),
        "value": value,
        "why": ("%s on %s: %d shares, bound by the %s (%d x %.2f = %s at the stop)"
                % (plan.get("setup", "setup"), universe.display(symbol), qty, decided_by,
                   qty, risk, store.rupees(max_loss))),
        "plan": {
            "entry": round(entry, 2),
            "plan_entry": plan_entry,
            "entry_why": entry_why,
            "stop": round(stop, 2),
            "target": round(target, 2),
            "risk_per_share": risk,
            "max_loss": max_loss,
            "reward_risk": reward_risk,
            "setup": plan.get("setup"),
            "entry_time": plan.get("entry_time"),
            "flat_by": plan.get("flat_by", cfg["flat_by"]),
            "stop_rule": plan.get("stop_rule"),
        },
    }
    result = {
        "as_of": clock.fmt_dt(clock.now_ist()),
        "date": today,
        "currency": cfg.get("currency", "INR"),
        "entry_used": round(entry, 2),
        "entry_why": entry_why,
        "fresh_price_age_min": fresh_age,
        "limits": [{"limit": name, "shares": shares, "math": how} for name, shares, how in limits],
        "decided_by": decided_by,
        "qty": qty,
        "value": value,
        "max_loss": max_loss,
        "reward_risk": reward_risk,
        "orders": [order],
        "note": ("A stop is an order, not a promise: in a fast market it can fill below the stop, "
                 "so the real loss can be larger than %s." % store.rupees(max_loss)),
    }
    if write:
        store.write("orders.json", result)
    return result
