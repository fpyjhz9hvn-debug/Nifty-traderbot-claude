"""Cost-basis lots, holding periods and harvest candidates.

Ported from `trader-tax` in github.com/jakeschincariol/trader-skill, with the
one rule that genuinely differs swapped out: the US pack's one-year long-term
line and its 30-day wash-sale window are US federal rules. For Indian listed
equity the holding-period line is **12 months**, and India has no wash-sale
rule of that shape at all -- so this reports the repurchase window as
information rather than disallowing anything.

**This is not tax advice, and it names no tax rate.** It does arithmetic on
lots: what you paid, what it is worth, how long you have held it, and what a
sale would realise. What any of that costs you is for a tax professional, and
the rates change.

Lots are built from the paper broker's own fills, so nothing is ever invented.
"""
from __future__ import annotations

import datetime as _dt

from . import clock, data, store, universe

LONG_TERM_MONTHS = 12
REPURCHASE_WINDOW_DAYS = 30


def _lots() -> dict:
    """FIFO lots per symbol, rebuilt from the fill ledger."""
    portfolio = store.read("portfolio.json") or {}
    open_lots: dict = {}
    counter: dict = {}
    for trade in portfolio.get("trades") or []:
        symbol = trade["symbol"]
        if trade["side"] == "buy":
            counter[symbol] = counter.get(symbol, 0) + 1
            open_lots.setdefault(symbol, []).append({
                "id": "%s-%d" % (universe.display(symbol), counter[symbol]),
                "date": trade["at"],
                "qty": trade["qty"],
                "price": trade["price"],
                "cost": round(trade["qty"] * trade["price"] + float(trade.get("charges") or 0), 2),
            })
        else:
            remaining = trade["qty"]
            queue = open_lots.get(symbol) or []
            while remaining > 0 and queue:
                lot = queue[0]
                take = min(lot["qty"], remaining)
                lot["qty"] -= take
                lot["cost"] = round(lot["cost"] * (lot["qty"] / (lot["qty"] + take)), 2) \
                    if (lot["qty"] + take) else 0.0
                remaining -= take
                if lot["qty"] <= 0:
                    queue.pop(0)
    return {s: [lot for lot in lots if lot["qty"] > 0] for s, lots in open_lots.items()}


def _term(bought: str, as_of: _dt.datetime):
    try:
        when = clock.parse_ist(bought)
    except (ValueError, TypeError):
        return None, None, None
    months = (as_of.year - when.year) * 12 + (as_of.month - when.month)
    if as_of.day < when.day:
        months -= 1
    long_term_on = when.replace(year=when.year + 1)
    return ("long" if months >= LONG_TERM_MONTHS else "short",
            (long_term_on.date() - as_of.date()).days,
            long_term_on.strftime("%Y-%m-%d"))


def report(write: bool = True) -> dict:
    now = clock.now_ist()
    lots = _lots()
    symbols = sorted(lots)
    quotes = data.quotes(symbols) if symbols else {}

    rows, totals = [], {"cost": 0.0, "value": 0.0, "unrealised": 0.0,
                        "short": 0.0, "long": 0.0}
    for symbol in symbols:
        price = (quotes.get(symbol) or {}).get("price")
        for lot in lots[symbol]:
            basis_per_share = lot["cost"] / lot["qty"] if lot["qty"] else 0.0
            value = (price or basis_per_share) * lot["qty"]
            gain = round(value - lot["cost"], 2)
            term, days_to_long, long_on = _term(lot["date"], now)
            rows.append({
                "id": lot["id"], "symbol": symbol, "display": universe.display(symbol),
                "bought": lot["date"], "qty": lot["qty"],
                "price_paid": round(basis_per_share, 2), "cost": lot["cost"],
                "last": price, "value": round(value, 2),
                "unrealised": gain,
                "unrealised_pct": round(gain / lot["cost"] * 100, 2) if lot["cost"] else 0.0,
                "term": term, "long_term_on": long_on, "days_to_long_term": days_to_long,
            })
            totals["cost"] += lot["cost"]
            totals["value"] += value
            totals["unrealised"] += gain
            totals[term or "short"] += gain

    realised = _realised(now)
    out = {
        "as_of": clock.fmt_dt(now),
        "lots": sorted(rows, key=lambda r: r["unrealised"]),
        "lot_count": len(rows),
        "totals": {k: round(v, 2) for k, v in totals.items()},
        "realised": realised,
        "long_term_months": LONG_TERM_MONTHS,
        "note": ("Not tax advice, and no tax rate is named anywhere here. For Indian listed "
                 "equity the long-term line is 12 months; what that costs you is for a tax "
                 "professional. Lots are rebuilt from the paper account's own fills."),
    }
    if write:
        store.write("taxlots.json", out)
    return out


def _realised(now) -> dict:
    """What has already been closed, split by holding period."""
    from . import performance
    trips = performance._round_trips(store.read("portfolio.json") or {})
    short = long = 0.0
    rows = []
    for trip in trips:
        term, _days, _on = _term(trip["entry_at"], clock.parse_ist(trip["exit_at"])
                                 if trip.get("exit_at") else now)
        if term == "long":
            long += trip["net"]
        else:
            short += trip["net"]
        rows.append({**trip, "term": term})
    return {
        "short_term": round(short, 2), "long_term": round(long, 2),
        "total": round(short + long, 2), "count": len(rows),
        "rows": list(reversed(rows))[:40],
    }


def harvest(min_loss: float = 500.0) -> dict:
    """Lots sitting on a loss worth more than the threshold."""
    data_report = report(write=False)
    candidates = []
    for lot in data_report["lots"]:
        if lot["unrealised"] >= -abs(min_loss):
            continue
        buy_back_after = None
        try:
            buy_back_after = (clock.now_ist()
                              + _dt.timedelta(days=REPURCHASE_WINDOW_DAYS)).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            pass
        candidates.append({**lot, "loss": lot["unrealised"],
                           "repurchase_note": (
                               "India has no US-style wash-sale rule, so a repurchase is not "
                               "disallowed. %s is simply 30 days out, if you want to keep a gap."
                               % buy_back_after)})
    return {
        "as_of": data_report["as_of"],
        "min_loss": min_loss,
        "candidates": candidates,
        "total_loss": round(sum(c["loss"] for c in candidates), 2),
        "message": ("%d lot(s) are down more than %s." % (len(candidates), store.rupees(min_loss))
                    if candidates else
                    "No lot is down more than %s." % store.rupees(min_loss)),
        "note": ("Not tax advice. Selling to book a loss is a decision about your tax position, "
                 "which this app knows nothing about, and it still costs the spread and the "
                 "charges."),
    }


def what_would_selling_realise(symbol: str, qty: int) -> dict:
    """FIFO: what a sale of this size would book, before you place it."""
    symbol = universe.normalise(symbol)
    lots = _lots().get(symbol) or []
    if not lots:
        return {"error": "No lots held for %s." % universe.display(symbol)}
    try:
        price = data.quote(symbol)["price"]
    except data.DataError as exc:
        return {"error": str(exc)}
    now = clock.now_ist()
    remaining, used, realised = int(qty), [], 0.0
    for lot in lots:
        if remaining <= 0:
            break
        take = min(lot["qty"], remaining)
        basis = lot["cost"] / lot["qty"] * take
        gain = take * price - basis
        term, _days, _on = _term(lot["date"], now)
        used.append({"id": lot["id"], "qty": take, "bought": lot["date"],
                     "basis": round(basis, 2), "gain": round(gain, 2), "term": term})
        realised += gain
        remaining -= take
    return {
        "symbol": symbol, "display": universe.display(symbol),
        "qty": int(qty) - remaining, "short": remaining,
        "price": price, "method": "FIFO (oldest lot first)",
        "lots_used": used,
        "realised": round(realised, 2),
        "short_term": round(sum(u["gain"] for u in used if u["term"] == "short"), 2),
        "long_term": round(sum(u["gain"] for u in used if u["term"] == "long"), 2),
        "note": ("Not tax advice. Your broker applies its own lot method unless you choose one "
                 "in their tax-lot setting, so the lots it actually sells may differ."),
    }
