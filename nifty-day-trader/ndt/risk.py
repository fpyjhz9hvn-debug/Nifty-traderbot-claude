"""Risk Manager (trader-risk, ported to NSE).

The gate between a proposal and the broker. A BLOCK is final for that order: it
is never shown for a yes and never reaches the broker.

Entries are checked against every limit. An exit -- an order that only reduces
what you already hold -- is checked for format and short selling only, because
getting out must always be possible. Every other rule becomes a warning there.

Each verdict is fingerprinted, so an edited order is not ready until it has
passed again, and yesterday's PASS does not count today.
"""
from __future__ import annotations

import hashlib
import json

from . import clock, discipline, store, universe


def fingerprint(order: dict) -> str:
    core = {
        "symbol": order.get("symbol"), "side": order.get("side"), "qty": order.get("qty"),
        "type": order.get("type"), "limit_price": order.get("limit_price"),
        "stop": order.get("stop_loss_stop_price"), "target": order.get("take_profit_limit_price"),
        "tif": order.get("time_in_force"),
    }
    blob = json.dumps(core, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _held(portfolio: dict, symbol: str) -> int:
    pos = (portfolio.get("positions") or {}).get(symbol) or {}
    return int(pos.get("qty", 0))


def check(orders: dict | None = None, write: bool = True) -> dict:
    cfg = store.config()
    payload = orders or store.read("orders.json") or {}
    order_list = payload.get("orders") or []
    portfolio = store.read("portfolio.json") or {"cash": float(cfg["budget"]), "positions": {}}
    log = store.read("state/broker.json") or {"entries": [], "orders": []}
    prices = (store.read("prices.json") or {}).get("prices") or {}
    price_as_of = (store.read("prices.json") or {}).get("as_of")
    now = clock.now_ist()
    today = now.strftime("%Y-%m-%d")

    todays = [o for o in (log.get("orders") or [])
              if str(o.get("date")) == today and o.get("status") == "placed"]
    todays_entries = [o for o in todays if o.get("kind") == "entry"]
    risked_today = sum(float(o.get("max_loss") or 0) for o in todays_entries)

    cash = float(portfolio.get("cash", cfg["budget"]))

    # The discipline layer, ported from claude-trading-skills. These are
    # account-level and market-level gates: they do not depend on the order, so
    # they are evaluated once and applied to every entry in the batch.
    breaker = discipline.circuit_breaker(now)
    heat = discipline.portfolio_heat()
    revenge = discipline.revenge_check(now)
    regime_state = None
    if cfg.get("use_regime_gate", True):
        from . import regime as regime_mod
        regime_state = regime_mod.cached(max_age_minutes=90)

    verdicts = []
    passed = 0

    for index, order in enumerate(order_list, start=1):
        symbol = universe.normalise(order.get("symbol") or "")
        qty = int(order.get("qty") or 0)
        side = (order.get("side") or "").lower()
        held = _held(portfolio, symbol)
        is_exit = side == "sell" and qty <= held and held > 0
        # A delivery order is a holding, not a day trade. The rules that exist
        # because a day trade must be flat by the close -- the entry window, a
        # required stop, reward:risk, the per-day trade count -- do not apply to
        # it. The rules about size, cash, concentration and realised damage do.
        is_delivery = (order.get("segment") == "delivery")
        reasons, warnings = [], []

        def block(text):
            reasons.append(text)

        def warn(text):
            warnings.append(text)

        note = block if not is_exit else warn

        # ---- format (always blocks)
        if not symbol:
            reasons.append("format: no symbol.")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell.")
        if qty <= 0 or qty != order.get("qty"):
            reasons.append("format: quantity must be a whole number of shares.")
        if order.get("type") == "limit" and not order.get("limit_price"):
            reasons.append("format: a limit order needs a limit price.")
        if order.get("time_in_force") not in ("day", "gtc"):
            reasons.append("format: time in force must be day or gtc.")
        if order.get("order_class") == "bracket" and side == "buy":
            if not order.get("stop_loss_stop_price") or not order.get("take_profit_limit_price"):
                reasons.append("format: a bracket order needs both a stop-loss and a take-profit.")

        # ---- asset
        if symbol.endswith(".NS") or symbol.startswith("^"):
            pass
        else:
            reasons.append("asset: only NSE cash-segment symbols (.NS) are allowed here.")
        if symbol.startswith("^"):
            reasons.append("asset: an index is not tradable in the cash segment.")

        # ---- short selling (always blocks)
        if side == "sell" and qty > held and not cfg.get("allow_short"):
            reasons.append("short: selling %d while you hold %d would go short, and short selling "
                           "is off." % (qty, held))

        plan = order.get("plan") or {}
        stop = order.get("stop_loss_stop_price") or plan.get("stop")
        target = order.get("take_profit_limit_price") or plan.get("target")

        entry_price = order.get("limit_price") or plan.get("entry")
        entry_why = "the limit price" if order.get("limit_price") else "the plan's entry"
        fresh = prices.get(symbol)
        if not order.get("limit_price") and fresh and price_as_of:
            try:
                age = (now - clock.parse_ist(price_as_of)).total_seconds() / 60
            except ValueError:
                age = 999
            if age < 15 and entry_price and float(fresh) > float(entry_price):
                entry_price, entry_why = float(fresh), "a fresh quote (%.0f min old), above the plan" % age
            elif age >= 15:
                warnings.append("The saved price is %.0f minutes old, so it was not used." % age)

        value = round(qty * float(entry_price or 0), 2)
        risk_per_share = (float(entry_price) - float(stop)) if (entry_price and stop) else None
        max_loss = round(qty * risk_per_share, 2) if risk_per_share else None
        reward_risk = (round((float(target) - float(entry_price)) / risk_per_share, 2)
                       if (target and risk_per_share and risk_per_share > 0) else None)

        if side == "buy":
            # ---- blocked list
            if universe.display(symbol) in [str(b).upper() for b in (cfg.get("blocked") or [])]:
                note("blocked: %s is on your blocked list." % universe.display(symbol))
            # ---- size
            if value < float(cfg["min_order_value"]):
                note("size: %s is under your %s minimum order."
                     % (store.rupees(value), store.rupees(cfg["min_order_value"])))
            if value > float(cfg["max_order_value"]):
                note("size: %s is over your %s maximum order."
                     % (store.rupees(value), store.rupees(cfg["max_order_value"])))
            # ---- position cap
            cap = float(cfg["budget"]) * float(cfg["max_position_pct"]) / 100
            existing = held * float(entry_price or 0)
            if existing + value > cap:
                note("position: %s in %s would be over your %g%% position cap (%s)."
                     % (store.rupees(existing + value), universe.display(symbol),
                        cfg["max_position_pct"], store.rupees(cap)))
            # ---- cash
            buffer_amount = float(cfg["budget"]) * float(cfg["cash_buffer_pct"]) / 100
            if value > cash - buffer_amount:
                note("cash: %s would leave %s, under your %g%% cash buffer (%s). No margin here."
                     % (store.rupees(value), store.rupees(cash - value),
                        cfg["cash_buffer_pct"], store.rupees(buffer_amount)))
            # ---- stop
            if cfg.get("require_stop") and not stop and not is_delivery:
                note("stop: this entry has no stop, and a stop is required.")
            if stop and entry_price and float(stop) >= float(entry_price):
                note("stop: %.2f is not below the entry %.2f." % (float(stop), float(entry_price)))
            # ---- loss
            if not is_delivery and max_loss is not None \
                    and max_loss > float(cfg["max_loss_per_trade"]):
                note("loss: %d shares x %.2f risk a share = %s at the stop, over your %s max loss "
                     "per trade." % (qty, risk_per_share, store.rupees(max_loss),
                                     store.rupees(cfg["max_loss_per_trade"])))
            # ---- reward:risk
            if not is_delivery and reward_risk is not None \
                    and reward_risk < float(cfg["min_reward_risk"]):
                note("reward:risk: %.2f, under your minimum %.2f."
                     % (reward_risk, cfg["min_reward_risk"]))
            # ---- daily loss
            if not is_delivery and max_loss is not None \
                    and risked_today + max_loss > float(cfg["max_daily_loss"]):
                note("daily loss: %s already at risk today plus %s is over your %s daily limit."
                     % (store.rupees(risked_today), store.rupees(max_loss),
                        store.rupees(cfg["max_daily_loss"])))
            # ---- trades per day (a day-trade limit; a rebalance is not day trading)
            if not is_delivery and len(todays_entries) >= int(cfg["max_trades_per_day"]):
                note("trades: %d entries already placed today, your limit is %d."
                     % (len(todays_entries), cfg["max_trades_per_day"]))
            if not is_delivery and len(todays) >= int(cfg["max_orders_per_day"]):
                note("daily limit: %d orders already placed today, your limit is %d."
                     % (len(todays), cfg["max_orders_per_day"]))
            # ---- window
            if not clock.is_trading_day(now.date(), store.holidays()):
                note("window: the NSE is closed today.")
            elif not clock.is_open(now, store.holidays()):
                note("window: the session is not running (09:15 to 15:30 IST).")
            elif is_delivery:
                pass  # a holding has no entry window inside the session
            else:
                before = cfg.get("no_entries_before")
                after = cfg.get("no_entries_after")
                if before and now.time() < clock.parse_hhmm(before):
                    note("window: no new entries before %s IST." % before)
                if after and now.time() > clock.parse_hhmm(after):
                    note("window: no new entries after %s IST." % after)
            # ---- circuit breaker (drawdown-circuit-breaker)
            if not breaker["allows_new_entries"]:
                note("circuit breaker: %s. %s" % (breaker["recommendation"],
                                                  breaker["rationale"]))
            # ---- portfolio heat (position-sizer). Heat is measured from stops,
            # and a holding has none, so it does not add to the day-trade heat.
            open_risk = max_loss or 0
            if not is_delivery and heat["heat"] + open_risk > heat["limit"]:
                note("portfolio heat: %s already at risk across open positions plus %s here is "
                     "over your %g%% heat cap (%s)."
                     % (store.rupees(heat["heat"]), store.rupees(open_risk),
                        heat["limit_pct"], store.rupees(heat["limit"])))
            # ---- revenge window (pre-trade-discipline-gate)
            if revenge.get("revenge_window"):
                note("revenge window: %s" % revenge["detail"])
            # ---- market regime (exposure-coach, ibd-distribution-day-monitor)
            if is_delivery:
                pass  # the regime gate is about timing a day trade, not owning a business
            elif regime_state is None and cfg.get("use_regime_gate", True):
                warnings.append("The market regime has not been read today, so the regime gate "
                                "could not be applied. Read it before taking a new entry.")
            elif regime_state is not None and not regime_state.get("allows_new_entries"):
                note("market regime: %s, %s. %s" % (regime_state["regime"],
                                                    regime_state["recommendation"],
                                                    regime_state["rationale"]))
            elif regime_state is not None and regime_state.get("stale"):
                warnings.append("The regime read is %.0f minutes old."
                                % regime_state.get("age_minutes", 0))

            # ---- duplicate
            seen = [v for v in verdicts if v["symbol"] == symbol and v["side"] == "buy"]
            if seen:
                note("duplicate: %s is already being bought in this batch."
                     % universe.display(symbol))
            if any(o.get("symbol") == symbol and o.get("side") == "buy" for o in todays_entries):
                note("duplicate: %s was already bought today." % universe.display(symbol))

        if cfg.get("paper") is not True:
            warnings.append("This config is not marked paper. This build only ever trades a "
                            "simulated account, but check your settings.")

        verdict = {
            "index": index,
            "symbol": symbol,
            "display": universe.display(symbol),
            "side": side,
            "qty": qty,
            "kind": "exit" if is_exit else "entry",
            "entry_price": round(float(entry_price), 2) if entry_price else None,
            "entry_why": entry_why,
            "stop": float(stop) if stop else None,
            "target": float(target) if target else None,
            "risk_per_share": round(risk_per_share, 2) if risk_per_share else None,
            "max_loss": max_loss,
            "value": value,
            "reward_risk": reward_risk,
            "verdict": "BLOCK" if reasons else "PASS",
            "reasons": reasons,
            "warnings": warnings,
            "fingerprint": fingerprint(order),
            "today_before_this": {
                "trades": len(todays_entries),
                "max_trades": int(cfg["max_trades_per_day"]),
                "risked": round(risked_today, 2),
                "max_daily_loss": float(cfg["max_daily_loss"]),
            },
        }
        verdicts.append(verdict)
        if not reasons:
            passed += 1
            # Check the next order as if this one had filled.
            if side == "buy":
                cash -= value
                risked_today += max_loss or 0
            else:
                cash += value

    out = {
        "as_of": clock.fmt_dt(now),
        "date": today,
        "checked": len(order_list),
        "passed": passed,
        "blocked": len(order_list) - passed,
        "cash_after_passed": round(cash, 2),
        "verdicts": verdicts,
        "discipline": {
            "circuit_breaker": breaker["recommendation"],
            "circuit_breaker_detail": breaker["rationale"],
            "heat": heat["heat"], "heat_pct": heat["heat_pct"], "heat_limit": heat["limit"],
            "revenge_window": revenge.get("revenge_window", False),
            "regime": regime_state and regime_state.get("regime"),
            "regime_recommendation": regime_state and regime_state.get("recommendation"),
            "regime_allows_entries": regime_state and regime_state.get("allows_new_entries"),
        },
        "note": ("A PASS only means the order is allowed to be shown to you. It is not a yes, and "
                 "it is not a recommendation."),
    }
    if write:
        store.write("state/risk.json", out)
    return out
