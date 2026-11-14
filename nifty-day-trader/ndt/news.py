"""News Monitor (trader-news, ported to NSE).

The US pack reads SEC EDGAR 8-Ks and Yahoo's US headline feed. Neither covers
Indian listed companies, and NSE's own corporate-announcements API answers 403
to anything that is not a browser session, so this module reads what is openly
available instead:

* Google News RSS, scoped to India (hl=en-IN, gl=IN), searched by company name.
* Yahoo Finance's per-ticker RSS headline feed, which carries some NSE names.
* Today's move, worked out from the stock's own 5-minute bars.

Headlines are aggregator headlines, not filings. Treat a headline as a reason to
look, never as a fact about the company, and check anything that matters against
the company's own filing on nseindia.com before you act on it.

It never places, sizes or scores an order. It tells you what is around the name
you are about to trade.
"""
from __future__ import annotations

import email.utils
import gzip
import html
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import clock, data, store, universe

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

# Aggregator quote pages that match any company name and say nothing.
JUNK_TITLE = re.compile(
    r"(stock price|share price|quote\s*&|price\s*&\s*chart|history\s*-\s*yahoo|"
    r"live nse|live bse|\bticker\b|price target calculator|technical analysis of)",
    re.IGNORECASE)
JUNK_SOURCE = re.compile(r"(upstox|tickertape|trendlyne|investing\.com|marketscreener|"
                         r"wallmine|stockinvest|munafasutra)", re.IGNORECASE)

TAGS = [
    ("results", r"\b(q[1-4]|quarter|quarterly|results|earnings|profit|pat\b|revenue|"
                r"topline|bottomline|ebitda|margin)\b"),
    ("order win", r"\b(order|contract|bags|wins|awarded|tender|loi\b)\b"),
    ("deal", r"\b(acquir|acquisition|merger|stake|divest|sells|buys|joint venture|jv\b|"
             r"takeover|open offer)\b"),
    ("guidance", r"\b(guidance|outlook|forecast|target price|raises|cuts|downgrade|upgrade)\b"),
    ("regulatory", r"\b(sebi|rbi|cci\b|nclt|court|probe|penalty|fine|notice|tax|gst|"
                   r"investigation|ban)\b"),
    ("capital", r"\b(dividend|buyback|bonus|split|rights issue|qip\b|fund rais|"
                r"block deal|bulk deal|stake sale|ipo\b)\b"),
    ("management", r"\b(ceo|cfo|managing director|chairman|resign|appoint|steps down|board)\b"),
    ("capex", r"\b(plant|capacity|expansion|capex|invest|project|factory|refinery)\b"),
]


def _fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-IN,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw


def _parse_rss(raw: bytes) -> list:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        def text(tag):
            node = item.find(tag)
            return html.unescape((node.text or "").strip()) if node is not None and node.text else ""
        source_node = item.find("source")
        out.append({
            "title": re.sub(r"\s+", " ", text("title")),
            "link": text("link"),
            "published_raw": text("pubDate"),
            "source": (html.unescape((source_node.text or "").strip())
                       if source_node is not None and source_node.text else ""),
            "summary": re.sub(r"<[^>]+>", " ", text("description"))[:400],
        })
    return out


def _to_ist(raw: str):
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw).astimezone(clock.IST)
    except (TypeError, ValueError):
        return None


def _tags(text: str) -> list:
    found = [name for name, pattern in TAGS if re.search(pattern, text, re.IGNORECASE)]
    return found


def _split_google_title(title: str):
    """Google News puts ' - Publisher' at the end of each title."""
    if " - " in title:
        head, _, tail = title.rpartition(" - ")
        if len(tail) < 45:
            return head.strip(), tail.strip()
    return title, ""


def headlines(symbol: str, hours: int = 24, limit: int = 20) -> dict:
    """Headlines around one NSE name, newest first, in IST."""
    symbol = universe.normalise(symbol)
    display = universe.display(symbol)
    name = universe.names().get(symbol, display)
    now = clock.now_ist()
    cutoff = now.timestamp() - hours * 3600
    sources_tried, errors, rows = [], [], []

    # 1. Google News, scoped to India.
    days = max(1, (hours + 23) // 24)
    query = '"%s" when:%dd' % (name, days)
    url = ("https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
           % urllib.parse.quote(query))
    sources_tried.append("Google News (India)")
    try:
        for item in _parse_rss(_fetch(url)):
            title, publisher = _split_google_title(item["title"])
            rows.append({**item, "title": title,
                         "source": item["source"] or publisher,
                         "feed": "Google News"})
    except Exception as exc:  # noqa: BLE001
        errors.append("Google News: %s" % exc)

    # 2. Yahoo's per-ticker feed. It carries some NSE names and not others.
    sources_tried.append("Yahoo Finance headlines")
    try:
        yurl = ("https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=IN&lang=en-IN"
                % urllib.parse.quote(symbol))
        for item in _parse_rss(_fetch(yurl)):
            rows.append({**item, "source": item["source"] or "Yahoo Finance", "feed": "Yahoo"})
    except Exception as exc:  # noqa: BLE001
        errors.append("Yahoo: %s" % exc)

    seen, clean = set(), []
    for row in rows:
        title = row["title"]
        if not title or JUNK_TITLE.search(title) or JUNK_SOURCE.search(row.get("source") or ""):
            continue
        key = re.sub(r"[^a-z0-9]", "", title.lower())[:70]
        if key in seen:
            continue
        seen.add(key)
        when = _to_ist(row["published_raw"])
        if when and when.timestamp() < cutoff:
            continue
        clean.append({
            "title": title,
            "source": row.get("source") or row.get("feed"),
            "feed": row["feed"],
            "link": row["link"],
            "published": when.strftime("%Y-%m-%d %H:%M") if when else None,
            "age_hours": round((now.timestamp() - when.timestamp()) / 3600, 1) if when else None,
            "tags": _tags(title + " " + (row.get("summary") or "")),
        })
    clean.sort(key=lambda r: r["published"] or "", reverse=True)
    clean = clean[:limit]

    move = _todays_move(symbol)
    tag_counts: dict = {}
    for row in clean:
        for tag in row["tags"]:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    flags = []
    if tag_counts.get("results"):
        flags.append("Results or earnings are in the headlines. A day trade is out before the "
                     "close, so it is not exposed to a report after hours, but the session itself "
                     "can gap and swing hard.")
    if tag_counts.get("regulatory"):
        flags.append("A regulatory headline is around this name. Those move prices in steps, not "
                     "in bars.")
    if move and move.get("change_pct") is not None and abs(move["change_pct"]) >= 3:
        flags.append("It is already %+.2f%% on the day: a lot of the move may have happened."
                     % move["change_pct"])
    if not clean:
        flags.append("No usable headline in the last %d hours. That is not the same as no news: "
                     "check the company's own filings on nseindia.com." % hours)

    return {
        "symbol": symbol,
        "display": display,
        "name": name,
        "as_of": clock.fmt_dt(now),
        "hours": hours,
        "move": move,
        "headlines": clean,
        "tags": tag_counts,
        "flags": flags,
        "sources": sources_tried,
        "errors": errors,
        "note": ("Aggregator headlines in India time, not exchange filings. NSE's own "
                 "corporate-announcements API refuses automated requests, so anything that "
                 "matters should be checked against the company's filing on nseindia.com."),
    }


def _todays_move(symbol: str) -> dict | None:
    try:
        quote = data.quote(symbol, ttl=30.0)
        bars = data.session_bars(symbol, interval="5m", ttl=45.0)
    except data.DataError:
        return None
    previous = quote.get("previous_close")
    price = quote["price"]
    out = {
        "price": price,
        "previous_close": previous,
        "change": round(price - previous, 2) if previous else None,
        "change_pct": round((price - previous) / previous * 100, 2) if previous else None,
        "quote_time": quote.get("quote_time"),
        "day_high": quote.get("day_high"),
        "day_low": quote.get("day_low"),
        "bars": len(bars),
    }
    if bars:
        session_open = bars[0]["o"]
        out["from_open_pct"] = round((price - session_open) / session_open * 100, 2)
        out["session_open"] = session_open
        highs = max(b["h"] for b in bars)
        lows = min(b["l"] for b in bars)
        out["range_pct"] = round((highs - lows) / session_open * 100, 2)
    return out


def watchlist_news(hours: int = 24, limit_symbols: int = 8) -> dict:
    """Headlines for whatever the scanner last wrote, a few names at a time."""
    saved = store.read("watchlist.json") or {}
    symbols, seen = [], set()
    for hit in saved.get("hits") or []:
        if hit["symbol"] not in seen:
            seen.add(hit["symbol"])
            symbols.append(hit["symbol"])
        if len(symbols) >= limit_symbols:
            break
    rows = []
    for symbol in symbols:
        try:
            rows.append(headlines(symbol, hours=hours, limit=5))
        except Exception as exc:  # noqa: BLE001
            rows.append({"symbol": symbol, "display": universe.display(symbol),
                         "headlines": [], "errors": [str(exc)], "flags": []})
    out = {"as_of": clock.fmt_dt(clock.now_ist()), "hours": hours, "rows": rows,
           "source": saved.get("mode")}
    store.write("news.json", out)
    return out
