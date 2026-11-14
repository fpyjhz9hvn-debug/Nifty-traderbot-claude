"""Does holding beat day trading on this account? Measure it.

The third skill pack (github.com/jakeschincariol/trader-skill) is not a day
trading pack at all. Its model is targets, a rebalance band, tax lots and
holding: few trades, each one deliberate. That is worth testing here for one
specific reason -- every measurement in this app so far says the cost of
trading, not the choice of setup, is what empties the account. Intraday, the
estimated charges came to roughly 9 % of the risk budget per round trip; a
position held for a month pays that once instead of twenty times.

So this runs three arms over the same daily window, on the same universe, with
the same money and the same charge model:

* **NIFTY buy and hold** -- the benchmark. You cannot buy the index in the cash
  segment, so treat it as the number to beat, not as a trade.
* **Equal-weight NIFTY 50, held** -- bought once, held, never rebalanced.
* **Momentum top N, rebalanced monthly** -- the v1 method: rank by 6-month
  return skipping the last month, hold only names above their 200-day average,
  equal weight the top N, rebalance monthly, keep a cash buffer.

And it splits the window the same way ndt/research.py does, so a result that
only works in the first half is visible as such.

Honest limits, which matter more here than in the intraday tests:
* Two years is two years. One bull market is not evidence that momentum works.
* Survivorship: today's NIFTY list applied to the past. Names that fell out of
  the index are missing, and that flatters every arm except the benchmark.
* Fills at the close, no slippage, no impact, no dividends in any arm.
"""
from __future__ import annotations

import math

from . import broker, clock, data, store, universe

BENCH = "^NSEI"


def _daily(symbols: list, rng: str = "2y") -> dict:
    """{symbol: {date: close}} from daily bars."""
    out = {}
    for symbol in symbols:
        try:
            chart = data.chart(symbol, rng=rng, interval="1d", ttl=3600)
        except data.DataError:
            continue
        series = {bar["date"]: bar["c"] for bar in chart["bars"] if bar["c"]}
        if len(series) > 200:
            out[symbol] = series
    return out


def _align(prices: dict, dates: list) -> dict:
    """Forward-fill one symbol onto the index's sessions; None before it listed."""
    out, last = {}, None
    for date in dates:
        if date in prices:
            last = prices[date]
        out[date] = last
    return out


def _sma(values: list, period: int):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _drawdown(curve: list) -> float:
    peak, worst = None, 0.0
    for _date, value in curve:
        peak = value if peak is None else max(peak, value)
        worst = max(worst, (peak - value) / peak * 100 if peak else 0.0)
    return round(worst, 2)


def _cagr(start: float, end: float, days: int) -> float:
    if start <= 0 or days <= 0:
        return 0.0
    years = days / 365.25
    return round(((end / start) ** (1 / years) - 1) * 100, 2) if years > 0 else 0.0


def _summary(label: str, curve: list, charges: float, trades: int, opening: float,
             turnover: float) -> dict:
    if not curve:
        return {"label": label, "trades": 0}
    start_date, _ = curve[0]
    end_date, end_value = curve[-1]
    days = (clock.parse_ist(end_date) - clock.parse_ist(start_date)).days
    return {
        "label": label,
        "start": start_date, "end": end_date,
        "opening": round(opening, 2),
        "final": round(end_value, 2),
        "total_return": round(end_value - opening, 2),
        "total_return_pct": round((end_value / opening - 1) * 100, 2),
        "cagr_pct": _cagr(opening, end_value, days),
        "max_drawdown_pct": _drawdown(curve),
        "trades": trades,
        "charges": round(charges, 2),
        "charges_pct_of_opening": round(charges / opening * 100, 2),
        "turnover": round(turnover, 2),
        "turnover_x": round(turnover / opening, 2),
        "curve": [{"t": d, "equity": round(v, 2)} for d, v in curve[::max(1, len(curve) // 180)]],
    }


def run(group: str = "nifty50", hold: int | None = None, rng: str = "2y",
        write: bool = True) -> dict:
    cfg = store.config()
    hold = int(hold or cfg.get("position_hold_count", 10))
    look = int(cfg.get("momentum_lookback_days", 126))
    skip = int(cfg.get("momentum_skip_days", 21))
    opening = float(cfg["budget"])
    buffer_pct = float(cfg["cash_buffer_pct"])

    raw = _daily(universe.symbols(group), rng)
    bench = _daily([BENCH], rng).get(BENCH) or {}
    if len(raw) < 20 or not bench:
        raise ValueError("Not enough daily history to run the comparison.")

    # The index's own sessions are the spine. Intersecting every symbol's dates
    # would let one newly listed name (TMCV and TMPV list only after the Tata
    # Motors demerger) truncate the whole window, which is how the first run of
    # this came back with 100 sessions instead of two years.
    dates = sorted(bench)
    series = {symbol: _align(prices, dates) for symbol, prices in raw.items()}
    if len(dates) < look + skip + 220:
        raise ValueError("Only %d sessions of index history: too few for a %d-day lookback "
                         "plus a 200-day average." % (len(dates), look))
    start_index = look + skip + 200

    # ---- arm 1: the benchmark, the index itself
    base = bench[dates[start_index]]
    bench_curve = [(d, opening * bench[d] / base) for d in dates[start_index:]]

    # ---- arm 2: equal weight, bought once and held
    symbols = sorted(series)
    sleeve = opening * (1 - buffer_pct / 100)
    per_name = sleeve / len(symbols)
    eq_shares, eq_charges, eq_turnover = {}, 0.0, 0.0
    first = dates[start_index]
    for symbol in symbols:
        price = series[symbol][first]
        if not price:
            continue  # not listed yet on the start date
        qty = math.floor(per_name / price)
        if qty < 1:
            continue
        eq_shares[symbol] = qty
        eq_turnover += qty * price
        eq_charges += broker.charges("buy", qty, price, cfg, segment="delivery")["total"]
    eq_cash = opening - eq_turnover - eq_charges
    eq_curve = [(d, eq_cash + sum(q * (series[s][d] or 0) for s, q in eq_shares.items()))
                for d in dates[start_index:]]

    # ---- arm 3: momentum top N, rebalanced monthly
    mo_curve, mo_charges, mo_trades, mo_turnover = _momentum_arm(
        series, dates, start_index, hold, look, skip, opening, buffer_pct, cfg)

    split = start_index + (len(dates) - start_index) // 2
    halves = {}
    for name, curve in (("benchmark", bench_curve), ("equal weight", eq_curve),
                        ("momentum", mo_curve)):
        mid = next((i for i, (d, _v) in enumerate(curve) if d >= dates[split]), 0)
        if not curve or mid <= 0 or mid >= len(curve) - 1:
            continue
        halves[name] = {
            "first_half_pct": round((curve[mid][1] / curve[0][1] - 1) * 100, 2),
            "second_half_pct": round((curve[-1][1] / curve[mid][1] - 1) * 100, 2),
        }

    arms = [
        _summary("NIFTY 50 buy and hold (benchmark, not tradable here)", bench_curve, 0.0, 0,
                 opening, 0.0),
        _summary("Equal-weight NIFTY 50, bought once and held", eq_curve, eq_charges,
                 len(eq_shares), opening, eq_turnover),
        _summary("Momentum top %d, rebalanced monthly" % hold, mo_curve, mo_charges, mo_trades,
                 opening, mo_turnover),
    ]

    intraday = store.read("backtest.json") or {}
    out = {
        "as_of": clock.fmt_dt(clock.now_ist()),
        "universe": group,
        "symbols": len(series),
        "sessions": len(dates) - start_index,
        "from": dates[start_index], "to": dates[-1],
        "hold": hold,
        "arms": arms,
        "halves": halves,
        "split_at": dates[split],
        "intraday_for_contrast": intraday and {
            "label": "Intraday opening-range breakout, every signal (60 sessions)",
            "trades": (intraday.get("ungated") or {}).get("trades"),
            "net": (intraday.get("ungated") or {}).get("net"),
            "charges": (intraday.get("ungated") or {}).get("charges"),
            "expectancy_r": (intraday.get("ungated") or {}).get("expectancy_r"),
        },
        "verdict": _verdict(arms, halves),
        "limits": [
            "Two years is two years, and this one contained a rising market. That is not "
            "evidence that momentum works; it is one window.",
            "Survivorship: today's NIFTY list applied to the past, which flatters every arm "
            "except the benchmark.",
            "Fills at the close, no slippage, no market impact, and no dividends in any arm.",
            "The charge model is the delivery one: no brokerage, STT on both sides, higher "
            "stamp duty. Your broker's rates will differ.",
        ],
    }
    if write:
        store.write("longtest.json", out)
    return out


def _momentum_arm(series, dates, start_index, hold, look, skip, opening, buffer_pct, cfg):
    cash = opening
    shares: dict = {}
    charges = turnover = 0.0
    trades = 0
    curve = []
    last_month = None
    symbols = sorted(series)

    for index in range(start_index, len(dates)):
        today = dates[index]
        month = today[:7]
        if month != last_month:
            last_month = month
            # Rank: return over `look` days, skipping the most recent `skip`.
            ranked = []
            for symbol in symbols:
                history = [p for p in (series[symbol][d] for d in dates[:index + 1]) if p]
                if len(history) < look + skip + 200:
                    continue  # this name alone is too young, the run carries on
                past = history[-(look + skip)]
                recent = history[-(skip + 1)]
                if past <= 0:
                    continue
                sma200 = _sma(history, 200)
                if not sma200 or history[-1] < sma200:
                    continue  # the v1 trend filter: no falling knives
                ranked.append((recent / past - 1, symbol))
            ranked.sort(reverse=True)
            want = {symbol for _score, symbol in ranked[:hold]}

            # Sell what is no longer wanted.
            for symbol in list(shares):
                if symbol in want:
                    continue
                price = series[symbol][today]
                qty = shares.pop(symbol)
                value = qty * price
                cost = broker.charges("sell", qty, price, cfg, segment="delivery")["total"]
                cash += value - cost
                charges += cost
                turnover += value
                trades += 1

            if want:
                equity = cash + sum(q * (series[s][today] or 0) for s, q in shares.items())
                sleeve = equity * (1 - buffer_pct / 100)
                target_each = sleeve / len(want)
                # Trim anything far above its target, then buy the rest.
                for symbol in list(shares):
                    price = series[symbol][today]
                    held_value = shares[symbol] * price
                    if held_value > target_each * 1.25:
                        sell_qty = math.floor((held_value - target_each) / price)
                        if sell_qty >= 1:
                            value = sell_qty * price
                            cost = broker.charges("sell", sell_qty, price, cfg,
                                                  segment="delivery")["total"]
                            shares[symbol] -= sell_qty
                            cash += value - cost
                            charges += cost
                            turnover += value
                            trades += 1
                for symbol in sorted(want):
                    price = series[symbol][today]
                    held_value = shares.get(symbol, 0) * price
                    room = target_each - held_value
                    if room <= 0:
                        continue
                    qty = math.floor(min(room, max(cash - opening * buffer_pct / 100, 0)) / price)
                    if qty < 1:
                        continue
                    value = qty * price
                    cost = broker.charges("buy", qty, price, cfg, segment="delivery")["total"]
                    if value + cost > cash:
                        continue
                    shares[symbol] = shares.get(symbol, 0) + qty
                    cash -= value + cost
                    charges += cost
                    turnover += value
                    trades += 1

        equity = cash + sum(q * (series[s][today] or 0) for s, q in shares.items())
        curve.append((today, equity))
    return curve, charges, trades, turnover


def _verdict(arms, halves) -> str:
    bench, equal, momentum = arms[0], arms[1], arms[2]
    bits = ["Over %s to %s: the NIFTY itself returned %+.2f%% (%.2f%% a year, worst drawdown "
            "%.2f%%). Equal weight held returned %+.2f%% on %d trades and %s of charges. "
            "Momentum top %s returned %+.2f%% on %d trades and %s of charges."
            % (bench["start"], bench["end"], bench["total_return_pct"], bench["cagr_pct"],
               bench["max_drawdown_pct"], equal["total_return_pct"], equal["trades"],
               store.rupees(equal["charges"]), momentum["label"].split()[2],
               momentum["total_return_pct"], momentum["trades"],
               store.rupees(momentum["charges"]))]
    best = max(arms, key=lambda a: a.get("total_return_pct", -999))
    if best is bench:
        bits.append("Neither active arm beat simply holding the index, which is the usual "
                    "result and the one worth taking seriously.")
    else:
        bits.append("%s came out ahead on this window." % best["label"])
    mo = halves.get("momentum")
    if mo:
        bits.append("Momentum returned %+.2f%% in the first half and %+.2f%% in the second; a "
                    "result that lives in one half is a window, not an edge."
                    % (mo["first_half_pct"], mo["second_half_pct"]))
    bits.append("Charges as a share of the account: %.2f%% for equal weight, %.2f%% for "
                "momentum, against the intraday arm's far higher turnover."
                % (equal["charges_pct_of_opening"], momentum["charges_pct_of_opening"]))
    return " ".join(bits)

