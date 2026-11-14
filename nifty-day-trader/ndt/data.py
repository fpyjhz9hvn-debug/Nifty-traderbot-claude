"""Free market data for NSE names, from Yahoo Finance's chart and spark
endpoints.

These are unofficial, free and delayed. They can lag, change shape or rate
limit you. Bars are 5-minute bars, never ticks. Nothing here is a live feed,
and this whole app only ever trades a simulated account, so a lagging price
costs nothing but a less realistic fill.
"""
from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import clock

UA = "nifty-day-trader/1.0 (educational paper trading; stdlib urllib)"
HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
_POLITE_GAP = 0.25  # seconds between requests

_lock = threading.Lock()
_last_call = [0.0]
_cache: dict = {}
_cache_lock = threading.Lock()


class DataError(Exception):
    pass


def _sleep_polite():
    with _lock:
        gap = time.time() - _last_call[0]
        if gap < _POLITE_GAP:
            time.sleep(_POLITE_GAP - gap)
        _last_call[0] = time.time()


def _get_json(path: str, params: dict, timeout: int = 25) -> dict:
    qs = urllib.parse.urlencode(params)
    last = None
    for attempt, host in enumerate(HOSTS):
        url = "https://%s%s?%s" % (host, path, qs)
        _sleep_polite()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = "HTTP %s from %s" % (exc.code, host)
            if exc.code == 429 and attempt == 0:
                time.sleep(5)
            continue
        except Exception as exc:  # noqa: BLE001 - network is network
            last = "%s: %s" % (type(exc).__name__, exc)
            continue
    raise DataError("Yahoo did not answer (%s)" % last)


def _cached(key: str, ttl: float, build):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = build()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def cache_clear():
    with _cache_lock:
        _cache.clear()


# --------------------------------------------------------------------------- bars

def _bars_from_result(result: dict, interval_min: int, drop_unclosed: bool = True) -> list:
    """Yahoo's parallel arrays -> a list of clean OHLCV bars in IST.

    Drops null closes, bars that are not on the interval grid (Yahoo appends the
    live tick, e.g. 12:57:24), and the bar that has not finished yet.
    """
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens, highs = quote.get("open") or [], quote.get("high") or []
    lows, closes = quote.get("low") or [], quote.get("close") or []
    vols = quote.get("volume") or []
    now = clock.now_ist()
    out = []
    for i, ts in enumerate(stamps):
        try:
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        except IndexError:
            continue
        if c is None or o is None or h is None or l is None:
            continue
        start = clock.from_epoch(ts)
        if start.second != 0:
            continue  # the live tick, not a bar
        mins = start.hour * 60 + start.minute
        open_mins = clock.SESSION_OPEN.hour * 60 + clock.SESSION_OPEN.minute
        if interval_min >= 5 and (mins - open_mins) % interval_min != 0:
            continue  # off the session's bar grid
        end = clock.bar_close_time(start, interval_min)
        if drop_unclosed and end > now:
            continue  # still forming
        v = vols[i] if i < len(vols) and vols[i] is not None else 0
        out.append({
            "t": int(ts),
            "start": start.strftime("%Y-%m-%d %H:%M"),
            "end": end.strftime("%H:%M"),
            "date": start.strftime("%Y-%m-%d"),
            "o": round(float(o), 2), "h": round(float(h), 2),
            "l": round(float(l), 2), "c": round(float(c), 2),
            "v": int(v),
        })
    return out


def chart(symbol: str, rng: str = "1d", interval: str = "5m", ttl: float = 45.0,
          drop_unclosed: bool = True) -> dict:
    """{'symbol', 'meta', 'bars': [...]} for one ticker."""
    key = "chart:%s:%s:%s:%s" % (symbol, rng, interval, drop_unclosed)

    def build():
        payload = _get_json(
            "/v8/finance/chart/" + urllib.parse.quote(symbol),
            {"range": rng, "interval": interval, "includePrePost": "false"},
        )
        chart_obj = payload.get("chart") or {}
        if chart_obj.get("error"):
            raise DataError("%s: %s" % (symbol, chart_obj["error"].get("description", "no data")))
        results = chart_obj.get("result") or []
        if not results:
            raise DataError("%s: Yahoo returned no chart" % symbol)
        result = results[0]
        meta = result.get("meta") or {}
        ivl_min = 1440 if interval.endswith("d") else int(interval.rstrip("m") or 5)
        return {
            "symbol": symbol,
            "meta": {
                "price": meta.get("regularMarketPrice"),
                "previous_close": meta.get("chartPreviousClose") or meta.get("previousClose"),
                "currency": meta.get("currency"),
                "exchange": meta.get("fullExchangeName"),
                "quote_time": (clock.from_epoch(meta["regularMarketTime"]).strftime("%Y-%m-%d %H:%M:%S")
                               if meta.get("regularMarketTime") else None),
                "day_high": meta.get("regularMarketDayHigh"),
                "day_low": meta.get("regularMarketDayLow"),
                "volume": meta.get("regularMarketVolume"),
            },
            "bars": _bars_from_result(result, ivl_min, drop_unclosed),
            "fetched_at": clock.now_ist().strftime("%Y-%m-%d %H:%M:%S"),
        }

    return _cached(key, ttl, build)


def daily_closes(symbols: list, rng: str = "3mo", ttl: float = 900.0) -> dict:
    """Spark: daily closes for many tickers, 20 a request."""
    out = {}
    for group in [symbols[i:i + 20] for i in range(0, len(symbols), 20)]:
        key = "spark:%s:%s" % (rng, ",".join(group))

        def build(group=group):
            return _get_json("/v7/finance/spark", {
                "symbols": ",".join(group), "range": rng, "interval": "1d",
            })

        try:
            payload = _cached(key, ttl, build)
        except DataError:
            continue
        for item in ((payload.get("spark") or {}).get("result") or []):
            sym = item.get("symbol")
            resp = (item.get("response") or [{}])[0]
            meta = resp.get("meta") or {}
            closes = [c for c in ((resp.get("indicators") or {}).get("quote") or [{}])[0].get("close", [])
                      if c is not None]
            if not closes:
                continue
            out[sym] = {
                "closes": [round(float(c), 2) for c in closes],
                "price": meta.get("regularMarketPrice") or closes[-1],
                "volume": meta.get("regularMarketVolume") or 0,
                "previous_close": meta.get("chartPreviousClose"),
            }
    return out


def intraday_spark(symbols: list, interval: str = "5m", ttl: float = 60.0) -> dict:
    """Spark: today's intraday closes for many tickers, 20 a request.

    Stage 1 of the opening-range scan: cheap closes for every name, so only the
    candidates cost a full chart request.
    """
    ivl_min = int(interval.rstrip("m") or 5)
    open_mins = clock.SESSION_OPEN.hour * 60 + clock.SESSION_OPEN.minute
    now = clock.now_ist()
    out = {}
    for group in [symbols[i:i + 20] for i in range(0, len(symbols), 20)]:
        key = "ispark:%s:%s" % (interval, ",".join(group))

        def build(group=group):
            return _get_json("/v7/finance/spark", {
                "symbols": ",".join(group), "range": "1d", "interval": interval,
            })

        try:
            payload = _cached(key, ttl, build)
        except DataError:
            continue
        for item in ((payload.get("spark") or {}).get("result") or []):
            sym = item.get("symbol")
            resp = (item.get("response") or [{}])[0]
            meta = resp.get("meta") or {}
            stamps = resp.get("timestamp") or []
            closes = ((resp.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            bars = []
            for i, ts in enumerate(stamps):
                if i >= len(closes) or closes[i] is None:
                    continue
                start = clock.from_epoch(ts)
                if start.second != 0:
                    continue
                if ((start.hour * 60 + start.minute) - open_mins) % ivl_min != 0:
                    continue
                if clock.bar_close_time(start, ivl_min) > now:
                    continue
                bars.append({"t": int(ts), "start": start.strftime("%H:%M"),
                             "end": clock.fmt_time(clock.bar_close_time(start, ivl_min)),
                             "c": round(float(closes[i]), 2),
                             "date": start.strftime("%Y-%m-%d")})
            if bars:
                out[sym] = {"bars": bars, "price": meta.get("regularMarketPrice") or bars[-1]["c"],
                            "volume": meta.get("regularMarketVolume") or 0}
    return out


def quote(symbol: str, ttl: float = 20.0) -> dict:
    """The latest delayed price for one ticker."""
    data = chart(symbol, rng="1d", interval="5m", ttl=ttl, drop_unclosed=False)
    meta = data["meta"]
    price = meta.get("price")
    if price is None and data["bars"]:
        price = data["bars"][-1]["c"]
    if price is None:
        raise DataError("%s: no price" % symbol)
    return {
        "symbol": symbol,
        "price": round(float(price), 2),
        "previous_close": meta.get("previous_close"),
        "quote_time": meta.get("quote_time") or data["fetched_at"],
        "day_high": meta.get("day_high"),
        "day_low": meta.get("day_low"),
        "source": "Yahoo Finance (free, delayed, unofficial)",
    }


def quotes(symbols: list, ttl: float = 20.0) -> dict:
    out = {}
    for sym in symbols:
        try:
            out[sym] = quote(sym, ttl=ttl)
        except Exception as exc:  # noqa: BLE001
            out[sym] = {"symbol": sym, "error": str(exc)}
    return out


def session_bars(symbol: str, date: str | None = None, interval: str = "5m",
                 ttl: float = 45.0, drop_unclosed: bool = True) -> list:
    """5-minute bars for one session. Today by default."""
    rng = "1d" if date in (None, clock.now_ist().strftime("%Y-%m-%d")) else "60d"
    data = chart(symbol, rng=rng, interval=interval, ttl=ttl, drop_unclosed=drop_unclosed)
    bars = data["bars"]
    if date:
        bars = [b for b in bars if b["date"] == date]
    elif bars:
        bars = [b for b in bars if b["date"] == bars[-1]["date"]]
    return bars


def sessions_60d(symbol: str, ttl: float = 1800.0) -> dict:
    """60 days of 5-minute bars, grouped by session date (oldest first)."""
    data = chart(symbol, rng="60d", interval="5m", ttl=ttl)
    grouped: dict = {}
    for bar in data["bars"]:
        grouped.setdefault(bar["date"], []).append(bar)
    return grouped


def validate(symbols: list) -> dict:
    """Which tickers still answer. Used once to prune the universe snapshot."""
    alive, dead = [], []
    found = daily_closes(symbols, rng="5d", ttl=3600.0)
    for sym in symbols:
        (alive if sym in found else dead).append(sym)
    return {"alive": alive, "dead": dead}
