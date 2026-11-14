"""A rolling news feed that keeps running, in or out of the session.

The News Monitor in ndt/news.py answers one question about one stock when you
ask it. This is the other half: a poller that keeps a de-duplicated feed warm in
the background, so what moved a name overnight or after the close is already
there when you open the app.

It runs whether or not the market is open, because news does not keep market
hours -- results land after the close, regulators publish in the evening, and a
Sunday headline is the reason Monday gaps.

Polite by design. Each cycle fetches one market-wide feed plus a couple of the
watched names on a rotating cursor, so a watch list of eight costs the same few
requests a cycle as a watch list of two; it just comes round less often.

What it is not: a wire. These are aggregator headlines with a publication time,
minutes to hours behind the event, and they are never exchange filings. Anything
that matters belongs checked against the company's own filing on nseindia.com.
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse

from . import clock, news, store, universe

MARKET_QUERY = '("Nifty 50" OR Sensex OR "Indian stock market") when:1d'
MARKET_KEY = "__market__"
KEEP_ITEMS = 250
SYMBOLS_PER_CYCLE = 2
MAX_WATCHED = 8
HOT_TAGS = ("results", "regulatory", "deal", "guidance")


def _item_id(title: str) -> str:
    key = re.sub(r"[^a-z0-9]", "", title.lower())[:80]
    return hashlib.sha1(key.encode()).hexdigest()[:14]


def _feed() -> dict:
    saved = store.read("newsfeed.json")
    if not isinstance(saved, dict):
        saved = {}
    saved.setdefault("items", [])
    saved.setdefault("cursor", 0)
    saved.setdefault("polls", 0)
    saved.setdefault("errors", [])
    return saved


def watch_symbols() -> list:
    """What the feed follows: what you hold, what you plan, what you just found."""
    out, seen = [], set()

    def add(symbol):
        if not symbol:
            return
        symbol = universe.normalise(symbol)
        if symbol not in seen and len(out) < MAX_WATCHED:
            seen.add(symbol)
            out.append(symbol)

    for symbol in (store.read("portfolio.json") or {}).get("positions") or {}:
        add(symbol)
    plan = store.read("plan.json") or {}
    add(plan.get("symbol"))
    for row in (store.read("candidates.json") or {}).get("candidates") or []:
        add(row.get("symbol"))
    for hit in (store.read("watchlist.json") or {}).get("hits") or []:
        add(hit.get("symbol"))
    return out


def _fetch_query(query: str, tag_symbol: str | None):
    url = ("https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
           % urllib.parse.quote(query))
    rows = []
    for raw in news._parse_rss(news._fetch(url)):
        title, publisher = news._split_google_title(raw["title"])
        source = raw.get("source") or publisher
        if not title or news.JUNK_TITLE.search(title) or news.JUNK_SOURCE.search(source or ""):
            continue
        when = news._to_ist(raw["published_raw"])
        rows.append({
            "id": _item_id(title),
            "title": title,
            "source": source or "Google News",
            "link": raw["link"],
            "published": when.strftime("%Y-%m-%d %H:%M") if when else None,
            "published_ts": when.timestamp() if when else 0,
            "tags": news._tags(title + " " + (raw.get("summary") or "")),
            "symbol": tag_symbol,
            "display": universe.display(tag_symbol) if tag_symbol else "market",
        })
    return rows


def poll(force_symbols: list | None = None, write: bool = True) -> dict:
    """One cycle: the market feed, plus the next couple of watched names."""
    feed = _feed()
    now = clock.now_ist()
    watched = force_symbols if force_symbols is not None else watch_symbols()
    existing = {item["id"]: item for item in feed["items"]}
    fresh, errors = [], []

    def ingest(rows):
        for row in rows:
            hit = existing.get(row["id"])
            if hit:
                # Same headline seen under another name: remember both.
                if row["symbol"] and row["symbol"] not in (hit.get("symbols") or []):
                    hit.setdefault("symbols", []).append(row["symbol"])
                continue
            row["symbols"] = [row["symbol"]] if row["symbol"] else []
            row["first_seen"] = clock.fmt_dt(now)
            row["unread"] = True
            existing[row["id"]] = row
            fresh.append(row)

    try:
        ingest(_fetch_query(MARKET_QUERY, None))
    except Exception as exc:  # noqa: BLE001
        errors.append("market feed: %s" % exc)

    polled = []
    if watched:
        cursor = feed.get("cursor", 0) % len(watched)
        for step in range(min(SYMBOLS_PER_CYCLE, len(watched))):
            symbol = watched[(cursor + step) % len(watched)]
            name = universe.names().get(symbol, universe.display(symbol))
            polled.append(symbol)
            try:
                ingest(_fetch_query('"%s" when:2d' % name, symbol))
            except Exception as exc:  # noqa: BLE001
                errors.append("%s: %s" % (universe.display(symbol), exc))
        feed["cursor"] = (cursor + len(polled)) % len(watched)

    items = sorted(existing.values(), key=lambda i: i.get("published_ts") or 0, reverse=True)
    feed["items"] = items[:KEEP_ITEMS]
    feed["as_of"] = clock.fmt_dt(now)
    feed["polls"] = feed.get("polls", 0) + 1
    feed["watching"] = [universe.display(s) for s in watched]
    feed["last_polled"] = [universe.display(s) for s in polled]
    feed["errors"] = errors
    feed["unread"] = sum(1 for i in feed["items"] if i.get("unread"))

    held = {universe.normalise(s) for s in
            ((store.read("portfolio.json") or {}).get("positions") or {})}
    feed["hot"] = [
        {"title": i["title"], "display": i["display"], "tags": i["tags"],
         "published": i["published"], "link": i["link"], "source": i["source"]}
        for i in fresh
        if (set(i["tags"]) & set(HOT_TAGS)) and set(i.get("symbols") or []) & held
    ]
    if write:
        store.write("newsfeed.json", feed)
    return {**feed, "new_this_poll": len(fresh), "new_items": fresh[:10]}


def recent(limit: int = 60, symbol: str | None = None, tag: str | None = None) -> dict:
    feed = _feed()
    items = feed["items"]
    if symbol:
        want = universe.normalise(symbol)
        items = [i for i in items if want in (i.get("symbols") or [])]
    if tag:
        items = [i for i in items if tag in (i.get("tags") or [])]
    counts: dict = {}
    for item in feed["items"]:
        for one in item.get("tags") or []:
            counts[one] = counts.get(one, 0) + 1
    return {
        "as_of": feed.get("as_of"),
        "polls": feed.get("polls", 0),
        "watching": feed.get("watching", []),
        "last_polled": feed.get("last_polled", []),
        "unread": sum(1 for i in feed["items"] if i.get("unread")),
        "total": len(feed["items"]),
        "tag_counts": counts,
        "items": items[:limit],
        "errors": feed.get("errors", []),
        "note": ("Aggregator headlines in India time, polled in the background whether or not "
                 "the market is open. Not a wire and not exchange filings: check anything that "
                 "matters on nseindia.com."),
    }


def mark_read(write: bool = True) -> dict:
    feed = _feed()
    for item in feed["items"]:
        item["unread"] = False
    if write:
        store.write("newsfeed.json", feed)
    return {"ok": True, "unread": 0}
