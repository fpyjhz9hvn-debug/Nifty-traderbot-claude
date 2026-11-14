"""The position sleeve: targets and the rebalancer.

Ported from github.com/jakeschincariol/trader-skill (the v1 pack), whose model
is the opposite of the day-trading one: decide what you want to own, buy it,
and only trade again when it drifts past a band.

* `trader-screener` -- rules against numbers, showing its work: every pass and
  every fail with the number and the threshold. The v1 rules are fundamental
  (revenue growth, margins, P/E) and come from SEC XBRL, which has nothing on
  Indian companies. Rather than invent fundamentals, the screens here run on
  what the free data actually supports: trend and momentum. That is a real
  limit, stated, not papered over.
* `trader-budget` weights mode -- the cash buffer off the top, weights
  renormalised, anything over the position cap pinned and its excess shared out.
* `trader-rebalance` -- value everything, compute drift in percentage points,
  trade only what is outside the band, sells before buys, skip tiny trades.

Everything it produces is a proposal in trader/orders.json. The Risk Manager
checks it and the paper broker asks for your yes, exactly as the intraday side
does. Orders carry `segment: delivery`, so they are charged as holdings rather
than as intraday trades.
"""
from __future__ import annotations

import math

from . import clock, data, scanner, store, universe

METHODS = {
    "equal": "equal weight across the list",
    "momentum": "top N by 6-month return skipping the last month, above the 200-day average",
    "manual": "weights you set yourself",
}


def _history(group: str, rng: str = "2y") -> dict:
    out = {}
    for symbol in universe.symbols(group):
        try:
            chart = data.chart(symbol, rng=rng, interval="1d", ttl=3600)
        except data.DataError:
            continue
        closes = [b["c"] for b in chart["bars"] if b["c"]]
        if len(closes) > 60:
            out[symbol] = closes
    return out


def build_targets(method: str = "momentum", group: str | None = None, hold: int | None = None,
                  weights: dict | None = None, write: bool = True) -> dict:
    """Decide what the sleeve should own, and show the work."""
    cfg = store.config()
    group = group or cfg.get("universe_group", "nifty50")
    hold = int(hold or cfg.get("position_hold_count", 10))
    look = int(cfg.get("momentum_lookback_days", 126))
    skip = int(cfg.get("momentum_skip_days", 21))
    names = universe.names()

    rows, rejected = [], []
    if method == "manual":
        total = sum(float(v) for v in (weights or {}).values()) or 1.0
        for raw_symbol, weight in (weights or {}).items():
            symbol = universe.normalise(raw_symbol)
            rows.append({"symbol": symbol, "display": universe.display(symbol),
                         "name": names.get(symbol, symbol),
                         "weight": round(float(weight) / total * 100, 2),
                         "why": "you set this weight"})
    else:
        history = _history(group)
        for symbol, closes in history.items():
            sma200 = (sum(closes[-200:]) / 200) if len(closes) >= 200 else None
            last = closes[-1]
            if method == "momentum":
                if len(closes) < look + skip + 1:
                    rejected.append({"display": universe.display(symbol),
                                     "why": "only %d sessions of history, needs %d"
                                            % (len(closes), look + skip + 1)})
                    continue
                if not sma200:
                    rejected.append({"display": universe.display(symbol),
                                     "why": "no 200-day average yet"})
                    continue
                if last < sma200:
                    rejected.append({"display": universe.display(symbol),
                                     "why": "%.2f is below its 200-day average %.2f"
                                            % (last, sma200)})
                    continue
                past, recent = closes[-(look + skip)], closes[-(skip + 1)]
                if past <= 0:
                    continue
                score = (recent / past - 1) * 100
                rows.append({"symbol": symbol, "display": universe.display(symbol),
                             "name": names.get(symbol, symbol), "score": round(score, 2),
                             "price": last, "sma200": round(sma200, 2),
                             "why": "%+.1f%% over the %d sessions ending %d ago, above its "
                                    "200-day average" % (score, look, skip)})
            else:
                rows.append({"symbol": symbol, "display": universe.display(symbol),
                             "name": names.get(symbol, symbol), "score": 0.0, "price": last,
                             "why": "equal weight"})
        if method == "momentum":
            rows.sort(key=lambda r: r["score"], reverse=True)
            for row in rows[hold:]:
                rejected.append({"display": row["display"],
                                 "why": "ranked outside the top %d (%+.1f%%)"
                                        % (hold, row["score"])})
            rows = rows[:hold]
        if rows:
            share = round(100.0 / len(rows), 4)
            for row in rows:
                row["weight"] = share

    # The position cap, applied the way trader-budget does: pin anything over
    # the cap and share its excess among the rest, so a capped name is not
    # bought back over the cap every month.
    cap = float(cfg["max_position_pct"])
    for _pass in range(6):
        over = [r for r in rows if r["weight"] > cap]
        if not over:
            break
        excess = sum(r["weight"] - cap for r in over)
        under = [r for r in rows if r["weight"] < cap]
        if not under:
            break
        room = sum(cap - r["weight"] for r in under)
        for row in over:
            row["weight"] = cap
            row["capped"] = True
        for row in under:
            row["weight"] = round(row["weight"] + excess * ((cap - row["weight"]) / room), 4)

    out = {
        "as_of": clock.fmt_dt(clock.now_ist()),
        "method": method,
        "method_note": METHODS.get(method, method),
        "universe": group,
        "hold": hold if method == "momentum" else len(rows),
        "targets": rows,
        "rejected": rejected[:40],
        "total_weight": round(sum(r["weight"] for r in rows), 2),
        "cash_buffer_pct": cfg["cash_buffer_pct"],
        "note": ("Weights for the stock sleeve, which is the account minus the %g%% cash "
                 "buffer. A screen is a filter, never a recommendation, and these rules run on "
                 "trend and momentum only: free data carries no Indian fundamentals."
                 % cfg["cash_buffer_pct"]),
    }
    if write:
        store.write("targets.json", out)
    return out


def drift(band: float | None = None, write: bool = False, sell_untracked: bool = False) -> dict:
    """The drift table, and the trades that would close it."""
    cfg = store.config()
    band = float(band if band is not None else cfg.get("rebalance_band_pct", 5.0))
    targets = store.read("targets.json") or {}
    rows = targets.get("targets") or []
    if not rows:
        return {"error": "No targets yet. Build them first.", "rows": []}

    portfolio = store.read("portfolio.json") or {}
    positions = portfolio.get("positions") or {}
    cash = float(portfolio.get("cash", cfg["budget"]))

    symbols = sorted({r["symbol"] for r in rows} | set(positions))
    quotes = data.quotes(symbols)
    prices = {s: (quotes.get(s) or {}).get("price") for s in symbols}
    missing = [universe.display(s) for s, p in prices.items() if not p]

    holdings_value = sum((prices.get(s) or pos["avg_price"]) * pos["qty"]
                         for s, pos in positions.items())
    account = cash + holdings_value
    buffer_amount = account * float(cfg["cash_buffer_pct"]) / 100
    # The sleeve gets its own slice of the account; the rest stays as cash for
    # the intraday side, which needs room for its own positions.
    sleeve_pct = float(cfg.get("position_sleeve_pct", 50.0))
    sleeve = max((account - buffer_amount) * sleeve_pct / 100, 0)

    table, trades = [], []
    target_by_symbol = {r["symbol"]: r for r in rows}
    for symbol in symbols:
        target = target_by_symbol.get(symbol)
        pos = positions.get(symbol)
        price = prices.get(symbol) or (pos or {}).get("avg_price")
        qty = pos["qty"] if pos else 0
        value = (price or 0) * qty
        current_pct = value / account * 100 if account else 0.0
        target_pct = (target["weight"] * sleeve / account) if (target and account) else 0.0
        drift_pts = round(current_pct - target_pct, 2)
        # The band in points, tightened for small targets. A 5-point band on a
        # 0.9 % position would never fire, so the effective band is the smaller
        # of the points band and a fraction of the target itself.
        relative = float(cfg.get("rebalance_relative_band_pct", 25.0))
        effective_band = min(band, max(target_pct * relative / 100, 0.05)) if target_pct \
            else band
        row = {
            "symbol": symbol, "display": universe.display(symbol),
            "name": universe.names().get(symbol, symbol),
            "in_targets": bool(target), "price": price, "qty": qty,
            "value": round(value, 2),
            "current_pct": round(current_pct, 2),
            "target_pct": round(target_pct, 2),
            "drift_pts": drift_pts,
            "band_used": round(effective_band, 2),
            "outside_band": abs(drift_pts) > effective_band,
        }
        table.append(row)

        if not price:
            continue
        if not target:
            if sell_untracked and qty > 0:
                trades.append(_order(symbol, "sell", qty, price,
                                     "not in the targets any more"))
            continue
        if abs(drift_pts) <= effective_band:
            continue
        want_value = target_pct / 100 * account
        delta_value = want_value - value
        delta_qty = int(math.floor(abs(delta_value) / price))
        if delta_qty < 1:
            continue
        order_value = delta_qty * price
        if order_value < float(cfg["min_order_value"]):
            row["skipped"] = "under the %s minimum order" % store.rupees(cfg["min_order_value"])
            continue
        if order_value > float(cfg["max_order_value"]):
            delta_qty = int(math.floor(float(cfg["max_order_value"]) / price))
            row["trimmed"] = "cut to the %s maximum order; run again after it fills" \
                             % store.rupees(cfg["max_order_value"])
            if delta_qty < 1:
                continue
        side = "buy" if delta_value > 0 else "sell"
        if side == "sell" and delta_qty > qty:
            delta_qty = qty
        if delta_qty < 1:
            continue
        trades.append(_order(symbol, side, delta_qty, price,
                             "%.2f points %s its %.2f%% target"
                             % (abs(drift_pts), "over" if drift_pts > 0 else "under",
                                target_pct)))

    # Sells first, then buys, and buys only spend cash above the buffer.
    sells = [t for t in trades if t["side"] == "sell"]
    buys = [t for t in trades if t["side"] == "buy"]
    cash_after_sells = cash + sum(t["value"] for t in sells)
    spendable = max(cash_after_sells - buffer_amount, 0)
    wanted = sum(t["value"] for t in buys)
    scaled = False
    if wanted > spendable and wanted > 0:
        ratio = spendable / wanted
        scaled = True
        for order in buys:
            order["qty"] = int(math.floor(order["qty"] * ratio))
            order["value"] = round(order["qty"] * order["limit_or_last"], 2)
        buys = [b for b in buys if b["qty"] >= 1
                and b["value"] >= float(cfg["min_order_value"])]
    ordered = sells + buys

    out = {
        "as_of": clock.fmt_dt(clock.now_ist()),
        "band_pts": band,
        "account": round(account, 2),
        "cash": round(cash, 2),
        "holdings_value": round(holdings_value, 2),
        "cash_buffer": round(buffer_amount, 2),
        "sleeve": round(sleeve, 2),
        "sleeve_pct": sleeve_pct,
        "rows": sorted(table, key=lambda r: -abs(r["drift_pts"])),
        "orders": ordered,
        "outside_band": sum(1 for r in table if r["outside_band"]),
        "untracked": [r["display"] for r in table if not r["in_targets"] and r["qty"]],
        "missing_prices": missing,
        "buys_scaled": scaled,
        "message": _message(table, ordered, band, scaled),
        "note": ("Proposals only, and a rebalance follows your own targets, not a forecast. "
                 "Nothing here says a rebalance improves returns: on the app's own measurement "
                 "(ndt/longtest.py) the arm that traded least did best."),
    }
    if write and ordered:
        store.write("orders.json", {
            "as_of": out["as_of"],
            "date": clock.now_ist().strftime("%Y-%m-%d"),
            "source": "rebalance",
            "orders": ordered,
            "note": "Rebalance proposals, sells first. Delivery segment.",
        })
    return out


def _order(symbol: str, side: str, qty: int, price: float, why: str) -> dict:
    return {
        "symbol": symbol, "display": universe.display(symbol),
        "side": side, "qty": int(qty), "type": "market", "limit_price": None,
        "time_in_force": "day", "order_class": "simple",
        "segment": "delivery",
        "take_profit_limit_price": None, "stop_loss_stop_price": None,
        "limit_or_last": price,
        "value": round(qty * price, 2),
        "why": "rebalance %s %s: %s" % (side, universe.display(symbol), why),
        "plan": {"entry": round(price, 2), "segment": "delivery", "rebalance": True},
    }


def _message(table, orders, band, scaled) -> str:
    outside = [r for r in table if r["outside_band"]]
    if not outside:
        return ("Nothing is outside the %.1f-point band. No trades: that is the normal answer "
                "and the cheap one." % band)
    bits = ["%d holding(s) drifted more than %.1f points." % (len(outside), band)]
    if orders:
        sells = sum(1 for o in orders if o["side"] == "sell")
        bits.append("%d sell(s) and %d buy(s) would close the gap." % (sells, len(orders) - sells))
    else:
        bits.append("No trade is big enough to be worth placing.")
    if scaled:
        bits.append("The buys were scaled down to fit the cash above your buffer.")
    return " ".join(bits)
