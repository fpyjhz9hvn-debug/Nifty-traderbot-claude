"""The paper broker (trader-broker, ported to NSE) -- and the paper account itself.

There is no brokerage account, no API key and no real money anywhere in this
file. Orders fill against free delayed Yahoo prices inside a simulated account
kept in trader/portfolio.json. It is a practice ledger, and nothing more.

An order still passes three gates, exactly as the skill pack insists:

  1. the Risk Manager passed THIS exact order today (matched by fingerprint),
  2. you typed an explicit yes for this one order,
  3. the app asks once more before it fills.

Fills are modelled honestly: a market order fills at the latest delayed quote,
a stop fills AT the stop (a real one can fill worse), and Indian intraday
charges are estimated and taken out of the account.
"""
from __future__ import annotations

import math
import re
import uuid

from . import clock, data, store, universe

YES = re.compile(r"^\s*(yes|y|yes please|confirm|confirmed|place it|do it|go ahead|yes buy|yes sell)\b",
                 re.IGNORECASE)


# ----------------------------------------------------------------- the account

def new_portfolio(opening: float) -> dict:
    now = clock.now_ist()
    return {
        "account": "PAPER-%s" % uuid.uuid4().hex[:8].upper(),
        "kind": "paper (simulated, no real money, no broker account)",
        "currency": "INR",
        "created_at": clock.fmt_dt(now),
        "opening_balance": round(float(opening), 2),
        "cash": round(float(opening), 2),
        "positions": {},
        "realised_pnl": 0.0,
        "charges_paid": 0.0,
        "trades": [],
        "equity_curve": [{"t": clock.fmt_dt(now), "equity": round(float(opening), 2)}],
    }


def portfolio() -> dict:
    p = store.read("portfolio.json")
    if not p:
        p = new_portfolio(store.config()["budget"])
        store.write("portfolio.json", p)
    return p


def reset_account(opening: float | None = None) -> dict:
    cfg = store.config()
    p = new_portfolio(opening if opening is not None else cfg["budget"])
    store.write("portfolio.json", p)
    store.write("state/broker.json", {"orders": [], "approvals": []})
    for name in ("orders.json", "plan.json", "entry.json", "timemachine.json", "state/risk.json"):
        store.delete(name)
    return p


def charges(side: str, qty: int, price: float, cfg: dict | None = None,
            segment: str = "intraday") -> dict:
    """An estimate of NSE equity charges, so the paper P&L is not fiction.

    Two segments, because they are taxed very differently and that difference
    turns out to matter more than any setup in this app:

    * **intraday** -- bought and sold the same day. Brokerage is the lower of
      0.03 % and a flat cap, STT 0.025 % on the sell side only, stamp duty
      0.003 % on buys.
    * **delivery** -- held overnight. Most discount brokers charge no brokerage,
      but STT is 0.1 % on BOTH sides and stamp duty 0.015 % on buys.

    Per trade, delivery costs more. Per year, it costs far less, because a
    position held for a month is one round trip rather than twenty.

    Rates are editable in the config; they change, and your own broker's differ.
    """
    cfg = cfg or store.config()
    if not cfg.get("apply_costs", True):
        return {"total": 0.0, "parts": {}, "segment": segment}
    turnover = qty * price
    if segment == "delivery":
        brokerage = min(turnover * float(cfg.get("delivery_brokerage_pct", 0.0)) / 100,
                        float(cfg.get("brokerage_cap", 20.0)))
        stt = turnover * float(cfg.get("stt_delivery_pct", 0.1)) / 100
        stamp = turnover * float(cfg.get("stamp_delivery_buy_pct", 0.015)) / 100 \
            if side == "buy" else 0.0
    else:
        brokerage = min(turnover * float(cfg.get("brokerage_pct", 0.03)) / 100,
                        float(cfg.get("brokerage_cap", 20.0)))
        stt = turnover * float(cfg.get("stt_sell_pct", 0.025)) / 100 if side == "sell" else 0.0
        stamp = turnover * float(cfg.get("stamp_buy_pct", 0.003)) / 100 if side == "buy" else 0.0
    exch = turnover * float(cfg.get("exchange_txn_pct", 0.00297)) / 100
    sebi = turnover * float(cfg.get("sebi_pct", 0.0001)) / 100
    gst = (brokerage + exch + sebi) * float(cfg.get("gst_pct", 18.0)) / 100
    parts = {"brokerage": round(brokerage, 2), "stt": round(stt, 2),
             "exchange": round(exch, 2), "sebi": round(sebi, 2),
             "stamp_duty": round(stamp, 2), "gst": round(gst, 2)}
    return {"total": round(sum(parts.values()), 2), "parts": parts, "segment": segment}


# ------------------------------------------------------------------- the tickets

def _log() -> dict:
    return store.read("state/broker.json") or {"orders": [], "approvals": []}


def _save_log(log: dict):
    store.write("state/broker.json", log)


def tickets() -> dict:
    """Every proposed order with its risk verdict, rendered as a plain ticket."""
    from . import risk as risk_mod

    cfg = store.config()
    payload = store.read("orders.json") or {}
    verdicts = store.read("state/risk.json") or {}
    today = clock.now_ist().strftime("%Y-%m-%d")
    by_fp = {v["fingerprint"]: v for v in (verdicts.get("verdicts") or [])} \
        if verdicts.get("date") == today else {}
    log = _log()
    approved = set()
    for entry in (log.get("approvals") or []):
        if entry.get("date") != today:
            continue
        if entry.get("fingerprint"):
            approved.add(entry["fingerprint"])
        approved.update(entry.get("fingerprints") or [])  # a basket approval
    placed = {o["fingerprint"] for o in (log.get("orders") or [])
              if o.get("date") == today and o.get("status") == "placed"}

    out = []
    for index, order in enumerate(payload.get("orders") or [], start=1):
        fp = risk_mod.fingerprint(order)
        verdict = by_fp.get(fp)
        plan = order.get("plan") or {}
        if fp in placed:
            state, why = "PLACED", "already filled in the paper account today."
        elif not verdict:
            state, why = "NOT READY", "the Risk Manager has not checked this exact order today."
        elif verdict["verdict"] == "BLOCK":
            state, why = "BLOCKED", "; ".join(verdict["reasons"])
        elif fp in approved:
            state, why = "APPROVED", "you said yes. Waiting on the final confirm."
        else:
            state, why = "READY", "Risk Manager PASS, waiting for your yes."
        out.append({
            "index": index,
            "fingerprint": fp,
            "state": state,
            "why": why,
            "order": order,
            "verdict": verdict,
            "account": "PAPER",
            "max_loss": plan.get("max_loss"),
            "lines": _ticket_lines(order, cfg),
        })
    return {"as_of": clock.fmt_dt(clock.now_ist()), "tickets": out,
            "account": portfolio()["account"],
            "note": "Paper account. No real order ever leaves this app."}


def _ticket_lines(order: dict, cfg: dict) -> list:
    plan = order.get("plan") or {}
    kind = "LIMIT %.2f" % order["limit_price"] if order.get("limit_price") else "MARKET"
    lines = ["%s %d %s, %s, good for today"
             % (order["side"].upper(), order["qty"], universe.display(order["symbol"]), kind)]
    if order.get("stop_loss_stop_price"):
        lines.append("stop-loss %.2f" % order["stop_loss_stop_price"])
    if order.get("take_profit_limit_price"):
        lines.append("take-profit %.2f" % order["take_profit_limit_price"])
    if plan.get("max_loss"):
        lines.append("max loss about %s if the stop fills at %.2f (it can fill worse in a fast market)"
                     % (store.rupees(plan["max_loss"]), order.get("stop_loss_stop_price") or 0))
    if order.get("why"):
        lines.append("why: " + order["why"])
    return lines


def approve(index: int, reply: str) -> dict:
    """Record an explicit yes for ONE order. Anything vague is not recorded."""
    found = next((t for t in tickets()["tickets"] if t["index"] == index), None)
    if not found:
        return {"ok": False, "message": "No ticket %d." % index}
    if found["state"] not in ("READY", "APPROVED"):
        return {"ok": False, "message": "Ticket %d is %s: %s" % (index, found["state"], found["why"])}
    text = (reply or "").strip()
    if not YES.match(text) or "all" in text.lower():
        return {"ok": False, "message":
                "Not recorded. That is not an explicit yes for this one order. Type yes."}
    log = _log()
    log.setdefault("approvals", []).append({
        "date": clock.now_ist().strftime("%Y-%m-%d"),
        "at": clock.fmt_dt(clock.now_ist()),
        "index": index,
        "fingerprint": found["fingerprint"],
        "reply": text,
        "symbol": found["order"]["symbol"],
        "qty": found["order"]["qty"],
        "side": found["order"]["side"],
    })
    _save_log(log)
    return {"ok": True, "message": "Recorded your yes for ticket %d (%s %d %s)."
            % (index, found["order"]["side"], found["order"]["qty"],
               universe.display(found["order"]["symbol"])), "fingerprint": found["fingerprint"]}


def approve_basket(reply: str) -> dict:
    """One yes for a whole basket of READY orders.

    The skill packs insist on one yes per order and never "yes to all", and
    that rule is right where a yes can reach a real broker. It cannot here:
    this account is a ledger in a JSON file with no order path out of the
    machine. So a basket approval exists, and it is honest about what it is --
    it is recorded as a basket, naming every order it covers by fingerprint, so
    the log never pretends you said yes forty-four times.

    It covers only the orders that are READY at this moment. Change any of them
    and its fingerprint changes, and the approval no longer covers it.
    """
    text = (reply or "").strip()
    if not YES.match(text):
        return {"ok": False, "message":
                "Not recorded. Type yes to approve the basket."}
    ready = [t for t in tickets()["tickets"] if t["state"] == "READY"]
    if not ready:
        return {"ok": False, "message":
                "No ticket is READY. Run the risk check first; a BLOCKED order never "
                "becomes part of a basket."}
    log = _log()
    now = clock.now_ist()
    value = sum(float(t["order"].get("value") or 0) for t in ready)
    log.setdefault("approvals", []).append({
        "date": now.strftime("%Y-%m-%d"),
        "at": clock.fmt_dt(now),
        "kind": "basket",
        "reply": text,
        "count": len(ready),
        "value": round(value, 2),
        "fingerprints": [t["fingerprint"] for t in ready],
        "orders": ["%s %d %s" % (t["order"]["side"], t["order"]["qty"],
                                 universe.display(t["order"]["symbol"])) for t in ready],
    })
    _save_log(log)
    return {"ok": True, "count": len(ready), "value": round(value, 2),
            "message": "Recorded one yes for a basket of %d order(s), %s in all."
                       % (len(ready), store.rupees(value))}


def place_basket() -> dict:
    """Fill every order the current basket approval covers."""
    results, filled, failed = [], 0, 0
    for ticket in tickets()["tickets"]:
        if ticket["state"] != "APPROVED":
            continue
        out = place(ticket["index"])
        results.append({"index": ticket["index"],
                        "display": universe.display(ticket["order"]["symbol"]),
                        "ok": out["ok"], "message": out["message"]})
        if out["ok"]:
            filled += 1
        else:
            failed += 1
    account = mark_to_market()
    return {
        "ok": filled > 0,
        "filled": filled, "failed": failed,
        "results": results,
        "cash": account["cash"], "equity": account["equity"],
        "open_positions": account["open_positions"],
        "message": ("Filled %d order(s) in the paper account, %d could not fill."
                    % (filled, failed)) if filled else
                   ("Nothing filled. %s" % (results[0]["message"] if results
                                            else "No approved order to fill.")),
    }


def place(index: int) -> dict:
    """Fill the order in the paper account. All three gates are re-checked here."""
    cfg = store.config()
    found = next((t for t in tickets()["tickets"] if t["index"] == index), None)
    if not found:
        return {"ok": False, "message": "No ticket %d." % index}
    if found["state"] == "PLACED":
        return {"ok": False, "message": "That order was already filled today."}
    if found["state"] != "APPROVED":
        return {"ok": False, "message":
                "Gate not met: %s. %s" % (found["state"], found["why"])}

    order = found["order"]
    symbol = order["symbol"]
    qty = int(order["qty"])
    try:
        live = data.quote(symbol, ttl=10.0)
        fill_price = float(order["limit_price"]) if order.get("limit_price") else live["price"]
        quote_time = live["quote_time"]
    except data.DataError as exc:
        return {"ok": False, "message": "No price for %s right now (%s). Nothing filled."
                % (universe.display(symbol), exc)}

    if order.get("limit_price") and live["price"] > float(order["limit_price"]):
        return {"ok": False, "message":
                "The market is at %.2f, above your %.2f limit. A limit buy would not fill."
                % (live["price"], order["limit_price"])}

    # The plan's own levels have to still make sense at the price you would fill
    # at. A trigger from hours ago is a different trade by now.
    stop = order.get("stop_loss_stop_price")
    target = order.get("take_profit_limit_price")
    if order["side"] == "buy" and stop and fill_price <= float(stop):
        return {"ok": False, "message":
                "Not filled: %s is at %.2f now, at or below the plan's stop %.2f. The setup has "
                "already failed, so this entry would be stopped out the moment it filled. Re-run "
                "the Entry Timer and the Exit Planner on the price now."
                % (universe.display(symbol), fill_price, float(stop))}
    if order["side"] == "buy" and target and fill_price >= float(target):
        return {"ok": False, "message":
                "Not filled: %s is at %.2f now, at or above the plan's target %.2f. The move this "
                "plan was waiting for has already happened."
                % (universe.display(symbol), fill_price, float(target))}

    segment = order.get("segment") or "intraday"
    result = _apply_fill(symbol, order["side"], qty, fill_price,
                         stop=order.get("stop_loss_stop_price"),
                         target=order.get("take_profit_limit_price"),
                         reason="%s: %s" % ("rebalance" if segment == "delivery" else "entry",
                                            order.get("why") or "manual"),
                         plan=order.get("plan") or {}, cfg=cfg, segment=segment)
    if not result["ok"]:
        return result

    log = _log()
    log.setdefault("orders", []).append({
        "id": "PO-" + uuid.uuid4().hex[:10].upper(),
        "date": clock.now_ist().strftime("%Y-%m-%d"),
        "at": clock.fmt_dt(clock.now_ist()),
        "fingerprint": found["fingerprint"],
        "kind": "entry" if order["side"] == "buy" else "exit",
        "symbol": symbol,
        "display": universe.display(symbol),
        "side": order["side"],
        "qty": qty,
        "type": order.get("type"),
        "fill_price": round(fill_price, 2),
        "quote_time": quote_time,
        "status": "placed",
        "stop": order.get("stop_loss_stop_price"),
        "target": order.get("take_profit_limit_price"),
        "max_loss": (order.get("plan") or {}).get("max_loss"),
        "charges": result["charges"]["total"],
        "account": "PAPER",
    })
    _save_log(log)
    return {"ok": True,
            "message": "FILLED in the paper account: %s %d %s at %.2f (delayed quote from %s). "
                       "Charges %s. The stop %.2f and target %.2f now sit on the position."
                       % (order["side"], qty, universe.display(symbol), fill_price, quote_time,
                          store.rupees(result["charges"]["total"]),
                          order.get("stop_loss_stop_price") or 0,
                          order.get("take_profit_limit_price") or 0),
            "fill": result}


def _apply_fill(symbol: str, side: str, qty: int, price: float, stop=None, target=None,
                reason: str = "", plan: dict | None = None, cfg: dict | None = None,
                segment: str = "intraday") -> dict:
    cfg = cfg or store.config()
    p = portfolio()
    cost = charges(side, qty, price, cfg, segment=segment)
    value = round(qty * price, 2)
    positions = p.setdefault("positions", {})
    now = clock.fmt_dt(clock.now_ist())

    if side == "buy":
        if value + cost["total"] > p["cash"]:
            return {"ok": False, "message": "Not enough paper cash: %s needed, %s available."
                    % (store.rupees(value + cost["total"]), store.rupees(p["cash"]))}
        pos = positions.get(symbol) or {"qty": 0, "avg_price": 0.0, "cost": 0.0}
        new_qty = pos["qty"] + qty
        pos["avg_price"] = round((pos["qty"] * pos["avg_price"] + value) / new_qty, 2)
        pos["qty"] = new_qty
        pos["cost"] = round(pos.get("cost", 0.0) + value + cost["total"], 2)
        pos["stop"] = float(stop) if stop else pos.get("stop")
        pos["target"] = float(target) if target else pos.get("target")
        pos["opened_at"] = pos.get("opened_at") or now
        pos["display"] = universe.display(symbol)
        pos["plan"] = plan or pos.get("plan")
        pos["segment"] = segment
        # A delivery holding is not closed by the intraday flat-by clock.
        pos["flat_by"] = None if segment == "delivery" else \
            ((plan or {}).get("flat_by") or cfg.get("flat_by"))
        positions[symbol] = pos
        p["cash"] = round(p["cash"] - value - cost["total"], 2)
        realised = None
    else:
        pos = positions.get(symbol)
        if not pos or pos["qty"] < qty:
            return {"ok": False, "message": "You do not hold %d %s in the paper account."
                    % (qty, universe.display(symbol))}
        gross = round((price - pos["avg_price"]) * qty, 2)
        realised = round(gross - cost["total"], 2)
        p["cash"] = round(p["cash"] + value - cost["total"], 2)
        p["realised_pnl"] = round(p.get("realised_pnl", 0.0) + realised, 2)
        pos["qty"] -= qty
        if pos["qty"] <= 0:
            positions.pop(symbol, None)
        else:
            positions[symbol] = pos

    p["charges_paid"] = round(p.get("charges_paid", 0.0) + cost["total"], 2)
    p.setdefault("trades", []).append({
        "at": now, "symbol": symbol, "display": universe.display(symbol),
        "side": side, "qty": qty, "price": round(price, 2), "value": value,
        "charges": cost["total"], "charge_parts": cost["parts"],
        "realised": realised, "reason": reason,
    })
    store.write("portfolio.json", p)
    return {"ok": True, "charges": cost, "realised": realised, "portfolio": p}


def flatten(symbol: str, qty: int | None = None, reason: str = "flat by the close") -> dict:
    """Write the sell ticket that closes a position. It still goes through risk."""
    symbol = universe.normalise(symbol)
    p = portfolio()
    pos = (p.get("positions") or {}).get(symbol)
    if not pos:
        return {"ok": False, "message": "No open %s position." % universe.display(symbol)}
    qty = int(qty or pos["qty"])
    order = {
        "symbol": symbol, "display": universe.display(symbol),
        "side": "sell", "qty": qty, "type": "market", "limit_price": None,
        "time_in_force": "day", "order_class": "simple",
        "take_profit_limit_price": None, "stop_loss_stop_price": None,
        "value": round(qty * pos["avg_price"], 2),
        "why": "close the %s position: %s" % (universe.display(symbol), reason),
        "plan": {"entry": pos["avg_price"], "exit_of": symbol},
    }
    store.write("orders.json", {
        "as_of": clock.fmt_dt(clock.now_ist()),
        "date": clock.now_ist().strftime("%Y-%m-%d"),
        "orders": [order],
        "note": "An exit. The Risk Manager never blocks an order that only gets you out.",
    })
    return {"ok": True, "message": "Sell ticket written for %d %s. Run the risk check, then say yes."
            % (qty, universe.display(symbol)), "order": order}


# ------------------------------------------------------- the bracket, bar by bar

def tick(force: bool = False) -> dict:
    """Walk open positions against the latest bars: stop first, then target.

    This is what a bracket order does at a real broker. Here it runs on free
    5-minute bars, so a fill is recorded at the bar that breached the level, at
    the level itself.
    """
    cfg = store.config()
    p = portfolio()
    events = []
    now = clock.now_ist()
    open_market = clock.is_open(now, store.holidays())
    if not (open_market or force):
        return {"as_of": clock.fmt_dt(now), "events": [], "market": "closed"}

    for symbol, pos in list((p.get("positions") or {}).items()):
        try:
            bars = data.session_bars(symbol, interval="5m", ttl=45.0)
        except data.DataError:
            continue
        if pos.get("segment") == "delivery":
            continue  # a holding has no bracket and is not flattened by the clock
        stop, target = pos.get("stop"), pos.get("target")
        opened = pos.get("opened_at", "")[-5:]
        after = [b for b in bars if b["end"] > opened] if opened else bars
        for bar in after:
            if stop and bar["l"] <= float(stop):
                res = _apply_fill(symbol, "sell", pos["qty"], float(stop),
                                  reason="stop-loss hit on the %s bar" % bar["end"], cfg=cfg)
                if res["ok"]:
                    events.append({"symbol": symbol, "display": universe.display(symbol),
                                   "event": "stop", "price": float(stop), "bar": bar["end"],
                                   "qty": pos["qty"], "realised": res["realised"]})
                break
            if target and bar["h"] >= float(target):
                res = _apply_fill(symbol, "sell", pos["qty"], float(target),
                                  reason="take-profit hit on the %s bar" % bar["end"], cfg=cfg)
                if res["ok"]:
                    events.append({"symbol": symbol, "display": universe.display(symbol),
                                   "event": "target", "price": float(target), "bar": bar["end"],
                                   "qty": pos["qty"], "realised": res["realised"]})
                break

    # Flat by the configured time, win or lose: no overnight hold.
    flat_by = cfg.get("flat_by", "15:20")
    if open_market and now.time() >= clock.parse_hhmm(flat_by):
        p = portfolio()
        for symbol, pos in list((p.get("positions") or {}).items()):
            if pos.get("segment") == "delivery":
                continue
            try:
                price = data.quote(symbol, ttl=30.0)["price"]
            except data.DataError:
                continue
            res = _apply_fill(symbol, "sell", pos["qty"], price,
                              reason="flat by %s, no overnight hold" % flat_by, cfg=cfg)
            if res["ok"]:
                events.append({"symbol": symbol, "display": universe.display(symbol),
                               "event": "flat_by", "price": price, "bar": flat_by,
                               "qty": pos["qty"], "realised": res["realised"]})

    if events:
        log = _log()
        for ev in events:
            log.setdefault("orders", []).append({
                "id": "PX-" + uuid.uuid4().hex[:10].upper(),
                "date": now.strftime("%Y-%m-%d"), "at": clock.fmt_dt(now),
                "fingerprint": "auto-" + ev["event"], "kind": "exit",
                "symbol": ev["symbol"], "display": ev["display"], "side": "sell",
                "qty": ev["qty"], "type": "market", "fill_price": ev["price"],
                "status": "placed", "realised": ev["realised"], "account": "PAPER",
                "note": "bracket %s" % ev["event"],
            })
        _save_log(log)
    return {"as_of": clock.fmt_dt(now), "events": events,
            "market": "open" if open_market else "closed"}


def mark_to_market() -> dict:
    """Value the account at the latest delayed prices."""
    p = portfolio()
    positions = p.get("positions") or {}
    rows, holdings_value, unrealised = [], 0.0, 0.0
    quotes = data.quotes(list(positions)) if positions else {}
    for symbol, pos in positions.items():
        q = quotes.get(symbol) or {}
        last = q.get("price") or pos["avg_price"]
        value = round(pos["qty"] * last, 2)
        pnl = round((last - pos["avg_price"]) * pos["qty"], 2)
        holdings_value += value
        unrealised += pnl
        risk_left = (round((last - float(pos["stop"])) * pos["qty"], 2)
                     if pos.get("stop") else None)
        rows.append({
            "symbol": symbol, "display": universe.display(symbol),
            "name": universe.names().get(symbol, symbol),
            "qty": pos["qty"], "avg_price": pos["avg_price"], "last": last,
            "value": value, "unrealised": pnl,
            "unrealised_pct": round((last / pos["avg_price"] - 1) * 100, 2) if pos["avg_price"] else 0,
            "stop": pos.get("stop"), "target": pos.get("target"),
            "opened_at": pos.get("opened_at"), "flat_by": pos.get("flat_by"),
            "to_stop": round(last - float(pos["stop"]), 2) if pos.get("stop") else None,
            "to_target": round(float(pos["target"]) - last, 2) if pos.get("target") else None,
            "open_risk": risk_left,
            "quote_time": q.get("quote_time"),
        })
    equity = round(p["cash"] + holdings_value, 2)
    opening = float(p.get("opening_balance") or equity)
    curve = p.setdefault("equity_curve", [])
    stamp = clock.fmt_dt(clock.now_ist())
    if not curve or curve[-1]["t"] != stamp:
        curve.append({"t": stamp, "equity": equity})
        p["equity_curve"] = curve[-500:]
        store.write("portfolio.json", p)
    today = clock.now_ist().strftime("%Y-%m-%d")
    realised_today = round(sum(float(t.get("realised") or 0) for t in (p.get("trades") or [])
                               if str(t.get("at", "")).startswith(today)), 2)
    return {
        "account": p["account"],
        "kind": p["kind"],
        "currency": "INR",
        "opening_balance": opening,
        "cash": p["cash"],
        "holdings_value": round(holdings_value, 2),
        "equity": equity,
        "unrealised": round(unrealised, 2),
        "realised_pnl": p.get("realised_pnl", 0.0),
        "realised_today": realised_today,
        "charges_paid": p.get("charges_paid", 0.0),
        "total_return": round(equity - opening, 2),
        "total_return_pct": round((equity / opening - 1) * 100, 2) if opening else 0,
        "positions": rows,
        "open_positions": len(rows),
        "equity_curve": p.get("equity_curve", [])[-200:],
        "created_at": p.get("created_at"),
        "as_of": stamp,
    }


def history(limit: int = 100) -> dict:
    p = portfolio()
    log = _log()
    trades = list(reversed(p.get("trades") or []))[:limit]
    # Pair buys with sells per symbol to show closed round trips.
    closed, open_lots = [], {}
    for trade in (p.get("trades") or []):
        sym = trade["symbol"]
        if trade["side"] == "buy":
            open_lots.setdefault(sym, []).append(trade)
        else:
            lots = open_lots.get(sym) or []
            entry = lots.pop(0) if lots else None
            if entry:
                closed.append({
                    "symbol": sym, "display": trade.get("display", universe.display(sym)),
                    "qty": trade["qty"], "entry": entry["price"], "exit": trade["price"],
                    "entry_at": entry["at"], "exit_at": trade["at"],
                    "reason": trade.get("reason"),
                    "gross": round((trade["price"] - entry["price"]) * trade["qty"], 2),
                    "charges": round(float(entry.get("charges") or 0) + float(trade.get("charges") or 0), 2),
                    "net": round((trade["price"] - entry["price"]) * trade["qty"]
                                 - float(entry.get("charges") or 0) - float(trade.get("charges") or 0), 2),
                })
    wins = [c for c in closed if c["net"] > 0]
    return {
        "trades": trades,
        "closed": list(reversed(closed))[:limit],
        "orders": list(reversed(log.get("orders") or []))[:limit],
        "approvals": list(reversed(log.get("approvals") or []))[:limit],
        "stats": {
            "round_trips": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "net": round(sum(c["net"] for c in closed), 2),
            "best": round(max((c["net"] for c in closed), default=0), 2),
            "worst": round(min((c["net"] for c in closed), default=0), 2),
            "avg": round(sum(c["net"] for c in closed) / len(closed), 2) if closed else 0,
        },
    }


def _floor2(value: float) -> float:
    return math.floor(value * 100) / 100
