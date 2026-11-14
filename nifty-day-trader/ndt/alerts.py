"""Price Alerts (trader-alerts, ported to NSE).

An alert is information, never an order. When a stop alert rings, this skill
sells nothing: the bracket on the position does that, and the alert only tells
you what happened and when it is time to be flat.

Each rule rings once. A rule carrying a date is watched on that IST date only,
so yesterday's stop never rings today.
"""
from __future__ import annotations

from . import clock, data, store, universe

TYPES = ("above", "below", "stop", "target", "pct_move", "time", "opening_bell")


def _rules() -> list:
    saved = store.read("alerts.json") or {"rules": []}
    return saved.get("rules") or []


def _save(rules: list):
    store.write("alerts.json", {"as_of": clock.fmt_dt(clock.now_ist()), "rules": rules})


def _state() -> dict:
    return store.read("state/alerts.json") or {"rang": {}}


def list_rules() -> dict:
    state = _state()
    today = clock.now_ist().strftime("%Y-%m-%d")
    rows = []
    for rule in _rules():
        rang = (state.get("rang") or {}).get(rule["id"])
        rows.append({**rule,
                     "rang_at": rang,
                     "active": (not rule.get("date") or rule["date"] == today) and not rang})
    return {"rules": rows, "as_of": clock.fmt_dt(clock.now_ist())}


def add(rule: dict) -> dict:
    kind = rule.get("type")
    if kind not in TYPES:
        return {"ok": False, "message": "Unknown alert type %r." % kind}
    symbol = universe.normalise(rule.get("symbol") or "") if rule.get("symbol") else None
    today = clock.now_ist().strftime("%Y-%m-%d")
    rid = rule.get("id") or "%s-%s-%s" % (
        (universe.display(symbol).lower() if symbol else "clock"), kind,
        str(rule.get("price") or rule.get("at") or rule.get("pct") or "x").replace(".", "-"))
    row = {
        "id": rid,
        "type": kind,
        "symbol": symbol,
        "display": universe.display(symbol) if symbol else None,
        "price": float(rule["price"]) if rule.get("price") not in (None, "") else None,
        "pct": float(rule["pct"]) if rule.get("pct") not in (None, "") else None,
        "at": rule.get("at"),
        "label": rule.get("label") or kind,
        "date": rule.get("date") or (today if kind in ("time", "stop", "target") else None),
        "rearm": bool(rule.get("rearm")),
        "created": clock.fmt_dt(clock.now_ist()),
    }
    rules = [r for r in _rules() if r["id"] != rid]
    rules.append(row)
    _save(rules)
    return {"ok": True, "rule": row,
            "message": "Watching %s: %s." % (row["display"] or "the clock", _describe(row))}


def _describe(rule: dict) -> str:
    if rule["type"] in ("above", "target"):
        return "rings at or above %.2f" % rule["price"]
    if rule["type"] in ("below", "stop"):
        return "rings at or below %.2f" % rule["price"]
    if rule["type"] == "pct_move":
        return "rings on a %.2f%% move from the previous close" % rule["pct"]
    if rule["type"] == "time":
        return "rings once the IST clock passes %s" % rule["at"]
    return "rings when the NSE opens"


def from_plan() -> dict:
    plan = store.read("plan.json")
    if not plan:
        return {"ok": False, "message": "No plan yet. Run the Exit Planner first."}
    made = [
        add({"type": "stop", "symbol": plan["symbol"], "price": plan["stop"],
             "label": "stop %.2f" % plan["stop"], "date": plan.get("date"),
             "id": "%s-plan-stop" % universe.display(plan["symbol"]).lower()}),
        add({"type": "target", "symbol": plan["symbol"], "price": plan["target"],
             "label": "target %.2f" % plan["target"], "date": plan.get("date"),
             "id": "%s-plan-target" % universe.display(plan["symbol"]).lower()}),
        add({"type": "time", "at": plan.get("flat_by", "15:20"),
             "label": "flat by %s" % plan.get("flat_by", "15:20"), "date": plan.get("date"),
             "id": "flat-by-%s" % plan.get("date")}),
    ]
    return {"ok": True, "rules": [m["rule"] for m in made],
            "message": "Three alerts set for %s: the stop %.2f, the target %.2f and flat by %s."
                       % (universe.display(plan["symbol"]), plan["stop"], plan["target"],
                          plan.get("flat_by", "15:20"))}


def reset(rule_id: str) -> dict:
    state = _state()
    (state.get("rang") or {}).pop(rule_id, None)
    store.write("state/alerts.json", state)
    return {"ok": True, "message": "%s can ring again." % rule_id}


def remove(rule_id: str) -> dict:
    _save([r for r in _rules() if r["id"] != rule_id])
    return {"ok": True, "message": "Removed %s." % rule_id}


def check(write_prices: bool = True) -> dict:
    now = clock.now_ist()
    today = now.strftime("%Y-%m-%d")
    rules = [r for r in _rules() if not r.get("date") or r["date"] == today]
    state = _state()
    rang = state.setdefault("rang", {})
    symbols = sorted({r["symbol"] for r in rules if r.get("symbol")})
    quotes = data.quotes(symbols) if symbols else {}
    if write_prices and quotes:
        store.write("prices.json", {
            "as_of": clock.fmt_dt(now),
            "prices": {s: q["price"] for s, q in quotes.items() if q.get("price")},
            "source": "Yahoo Finance (free, delayed, unofficial)",
        })

    fired = []
    open_now = clock.is_open(now, store.holidays())
    for rule in rules:
        if rang.get(rule["id"]) and not rule.get("rearm"):
            continue
        quote = quotes.get(rule.get("symbol")) if rule.get("symbol") else None
        price = (quote or {}).get("price")
        hit, text = False, ""
        if rule["type"] in ("above", "target") and price is not None:
            hit = price >= rule["price"]
            text = "%s is %.2f, at or above %s %.2f." % (rule["display"], price, rule["label"],
                                                         rule["price"])
        elif rule["type"] in ("below", "stop") and price is not None:
            hit = price <= rule["price"]
            text = "%s is %.2f, at or below %s %.2f." % (rule["display"], price, rule["label"],
                                                         rule["price"])
        elif rule["type"] == "pct_move" and price is not None:
            ref = (quote or {}).get("previous_close")
            if ref:
                move = (price - ref) / ref * 100
                hit = abs(move) >= abs(rule["pct"])
                text = "%s has moved %+.2f%% from the previous close %.2f (now %.2f)." % (
                    rule["display"], move, ref, price)
        elif rule["type"] == "time":
            hit = clock.is_trading_day(now.date(), store.holidays()) and \
                now.time() >= clock.parse_hhmm(rule["at"])
            text = ("It is %s IST (set for %s). If anything is still open, close it: the paper "
                    "broker's flatten builds the sell ticket, then the risk check, then your yes."
                    % (clock.fmt_time(now), rule["at"]))
        elif rule["type"] == "opening_bell":
            hit = open_now
            text = "The NSE is open (09:15 IST)."
        if hit:
            rang[rule["id"]] = clock.fmt_dt(now)
            fired.append({"id": rule["id"], "label": rule["label"], "text": text,
                          "symbol": rule.get("symbol"), "at": clock.fmt_dt(now)})
    store.write("state/alerts.json", state)
    return {
        "as_of": clock.fmt_dt(now),
        "market": clock.status(now, store.holidays()),
        "watched": len(rules),
        "fired": fired,
        "quotes": {s: q.get("price") for s, q in quotes.items()},
        "message": ("%d alert(s) rang." % len(fired)) if fired else
                   ("Nothing hit. %d rule(s) watched today." % len(rules)),
    }
