"""NIFTY Day Trader -- local web app.

    python app.py            # http://127.0.0.1:8777
    python app.py --port 9000 --no-browser

Standard library only: http.server for the API and the page, urllib for Yahoo.
Everything it trades is a simulated paper account in trader/portfolio.json.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ndt import (alerts, backtest, broker, budget, clock, data, discipline,
                 entry as entry_mod, exitplan, news as news_mod, newsfeed, performance, regime,
                 invest, longtest, research, risk, scanner, store, taxlots, timemachine,
                 universe, volatility, watcher)

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"

_watch_events: list = []
_watch_lock = threading.Lock()


def _record(kind: str, text: str, extra=None):
    with _watch_lock:
        _watch_events.insert(0, {"at": clock.fmt_dt(clock.now_ist()), "kind": kind,
                                 "text": text, "extra": extra})
        del _watch_events[60:]


# --------------------------------------------------------------------- the API

def api_state(_params, _body):
    cfg = store.config()
    account = broker.mark_to_market()
    market = clock.status(holidays=store.holidays())
    watchlist = store.read("watchlist.json") or {}
    regime_state = regime.cached(max_age_minutes=90)
    breaker = discipline.circuit_breaker()
    heat = discipline.portfolio_heat()
    plan = store.read("plan.json")
    entry_saved = store.read("entry.json")
    orders = store.read("orders.json")
    verdicts = store.read("state/risk.json")
    with _watch_lock:
        events = list(_watch_events[:20])
    return {
        "version": "1.0.0",
        "market": market,
        "account": account,
        "config": cfg,
        "chain": {
            "watchlist": {"mode": watchlist.get("mode"), "hits": len(watchlist.get("hits") or []),
                          "as_of": watchlist.get("as_of")},
            "entry": entry_saved and {"symbol": entry_saved.get("display"),
                                      "status": entry_saved.get("status"),
                                      "entry": entry_saved.get("entry"),
                                      "entry_time": entry_saved.get("entry_time")},
            "plan": plan and {"symbol": plan.get("display"), "entry": plan.get("entry"),
                              "stop": plan.get("stop"), "target": plan.get("target"),
                              "reward_risk": plan.get("reward_risk")},
            "orders": orders and {"qty": orders.get("qty"), "value": orders.get("value"),
                                  "max_loss": orders.get("max_loss")},
            "risk": verdicts and {"passed": verdicts.get("passed"),
                                  "blocked": verdicts.get("blocked"),
                                  "date": verdicts.get("date")},
        },
        "discipline": {
            "regime": regime_state and {
                "regime": regime_state["regime"], "risk_level": regime_state["risk_level"],
                "recommendation": regime_state["recommendation"],
                "exposure_ceiling_pct": regime_state["exposure_ceiling_pct"],
                "allows_new_entries": regime_state["allows_new_entries"],
                "stale": regime_state.get("stale"), "age_minutes": regime_state.get("age_minutes"),
                "rationale": regime_state["rationale"]},
            "circuit_breaker": {"recommendation": breaker["recommendation"],
                                "rationale": breaker["rationale"],
                                "metrics": breaker["metrics"], "limits": breaker["limits"]},
            "heat": {"heat": heat["heat"], "heat_pct": heat["heat_pct"],
                     "limit": heat["limit"], "limit_pct": heat["limit_pct"],
                     "over": heat["over"]},
        },
        "events": events,
        "disclaimer": ("Paper trading only: a simulated account, free delayed data and arithmetic. "
                       "Not financial advice. Most people who day trade lose money."),
    }


def api_clock(_p, _b):
    return clock.status(holidays=store.holidays())


def api_universe(_p, _b):
    names = universe.names()
    return {
        "groups": {key: [{"symbol": s, "display": universe.display(s), "name": names.get(s, s)}
                         for s, _ in rows] for key, rows in universe.GROUPS.items()},
        "index": universe.INDEX,
        "counts": {k: len(v) for k, v in universe.GROUPS.items()},
    }


def api_quotes(params, _b):
    symbols = [universe.normalise(s) for s in (params.get("symbols", [""])[0]).split(",") if s]
    if not symbols:
        symbols = [universe.INDEX["symbol"]]
    return {"as_of": clock.fmt_dt(clock.now_ist()), "quotes": data.quotes(symbols)}


def api_chart(params, _b):
    symbol = universe.normalise(params.get("symbol", ["^NSEI"])[0])
    rng = params.get("range", ["1d"])[0]
    interval = params.get("interval", ["5m"])[0]
    payload = data.chart(symbol, rng=rng, interval=interval, ttl=30.0)
    or_minutes = int(store.config().get("or_minutes", 15))
    bars = payload["bars"]
    today = bars[-1]["date"] if bars else None
    session = [b for b in bars if b["date"] == today] if rng == "1d" else bars
    rng_obj = scanner.opening_range(session, or_minutes) if rng == "1d" else None
    plan = store.read("plan.json") or {}
    return {
        "symbol": symbol,
        "display": universe.display(symbol),
        "name": universe.names().get(symbol, symbol),
        "meta": payload["meta"],
        "bars": session,
        "opening_range": rng_obj,
        "plan": plan if plan.get("symbol") == symbol else None,
        "position": (broker.portfolio().get("positions") or {}).get(symbol),
        "source": "Yahoo Finance (free, delayed, unofficial)",
    }


def api_scan(_p, body):
    mode = (body.get("mode") or "orb").lower()
    group = body.get("group") or store.config().get("universe_group", "nifty50")
    or_minutes = int(body.get("or_minutes") or store.config().get("or_minutes", 15))
    min_price = float(body.get("min_price") or 0)
    if mode == "daily":
        out = scanner.scan_daily(group=group, min_price=min_price)
    else:
        out = scanner.scan_orb(group=group, or_minutes=or_minutes, min_price=min_price)
    _record("scan", "%s scan over %s: %d hit(s)." % (mode, group, len(out.get("hits") or [])))
    return out


def api_watchlist(_p, _b):
    return store.read("watchlist.json") or {"hits": [], "mode": None}


def api_news(_p, body):
    out = news_mod.headlines(body["symbol"], hours=int(body.get("hours") or 24))
    store.write("news.json", out)
    _record("news", "%s: %d headline(s) in the last %dh."
            % (out["display"], len(out["headlines"]), out["hours"]))
    return out


def api_feed(params, body):
    body = body or {}
    return newsfeed.recent(
        limit=int(body.get("limit") or (params.get("limit", [60])[0])),
        symbol=body.get("symbol") or (params.get("symbol", [None])[0]),
        tag=body.get("tag") or (params.get("tag", [None])[0]),
    )


def api_feed_poll(_p, _b):
    out = newsfeed.poll()
    _record("news", "Feed polled: %d new headline(s); watching %s."
            % (out["new_this_poll"], ", ".join(out.get("watching") or []) or "the market only"))
    return newsfeed.recent()


def api_feed_read(_p, _b):
    return newsfeed.mark_read()


def api_news_watchlist(_p, body):
    return news_mod.watchlist_news(hours=int((body or {}).get("hours") or 24))


def api_regime(_p, body):
    cached = regime.cached() if not (body or {}).get("refresh") else None
    out = cached or regime.assess(group=(body or {}).get("group") or "nifty50")
    _record("regime", "%s, %s (risk %s, ceiling %s%%)."
            % (out["regime"], out["recommendation"], out["risk_level"],
               out["exposure_ceiling_pct"]))
    return out


def api_discipline(_p, _b):
    return {
        "circuit_breaker": discipline.circuit_breaker(),
        "heat": discipline.portfolio_heat(),
        "revenge": discipline.revenge_check(),
        "gate": discipline.gate(),
        "settings": discipline.settings(),
    }


def api_performance(_p, _b):
    return performance.report()


def api_backtest(_p, body):
    out = backtest.run(group=(body or {}).get("group") or "nifty50",
                       target_r=(body or {}).get("target_r"),
                       stop_rule=(body or {}).get("stop_rule") or "or-mid")
    _record("backtest", out["verdict"][:160])
    return out


def api_sweep(_p, body):
    out = backtest.sweep(group=(body or {}).get("group") or "nifty50")
    _record("backtest", out["verdict"][:160])
    return out


def api_backtest_saved(_p, _b):
    return {"backtest": store.read("backtest.json"), "sweep": store.read("sweep.json"),
            "research": store.read("research.json")}


def api_watch(_p, body):
    if (body or {}).get("saved"):
        return watcher.saved()
    out = watcher.scan(force=bool((body or {}).get("force")))
    _record("watch", out.get("message", "")[:160])
    return out


def api_watch_saved(_p, _b):
    return watcher.saved()


def api_targets(_p, body):
    body = body or {}
    if body.get("saved"):
        return store.read("targets.json") or {"targets": []}
    out = invest.build_targets(method=body.get("method") or "momentum",
                               hold=int(body["hold"]) if body.get("hold") else None,
                               weights=body.get("weights"))
    _record("invest", "Targets rebuilt (%s): %d names." % (out["method"], len(out["targets"])))
    return out


def api_drift(_p, body):
    body = body or {}
    out = invest.drift(band=float(body["band"]) if body.get("band") else None,
                       write=bool(body.get("write")),
                       sell_untracked=bool(body.get("sell_untracked")))
    if body.get("write"):
        _record("invest", "Rebalance proposals written: %d order(s)." % len(out.get("orders", [])))
    return out


def api_taxlots(_p, body):
    body = body or {}
    if body.get("harvest"):
        return taxlots.harvest(min_loss=float(body.get("min_loss") or 500))
    if body.get("sell_symbol"):
        return taxlots.what_would_selling_realise(body["sell_symbol"], int(body.get("qty") or 0))
    return taxlots.report()


def api_longtest(_p, body):
    body = body or {}
    if body.get("saved"):
        return store.read("longtest.json") or {}
    out = longtest.run(group=body.get("group") or "nifty50",
                       hold=int(body["hold"]) if body.get("hold") else None)
    _record("invest", out["verdict"][:160])
    return out


def api_research(_p, body):
    out = research.run(group=(body or {}).get("group") or "nifty50",
                       target_r=(body or {}).get("target_r"))
    _record("research", out["verdict"][:160])
    return out


def api_movers(_p, body):
    out = volatility.rank(
        group=(body.get("group") or store.config().get("universe_group", "nifty50")),
        want_sessions=int(body.get("sessions") or 20),
        min_turnover=float(body.get("min_turnover") or 0),
    )
    _record("movers", "Ranked %d names on %d sessions of 5-minute bars."
            % (out["ranked"], out["sessions"]))
    return out


def api_movers_saved(_p, _b):
    return store.read("movers.json") or {"rows": [], "ranked": 0}


def api_entry(_p, body):
    out = entry_mod.check(
        symbol=body["symbol"],
        setup=body.get("setup") or "orb",
        level=float(body["level"]) if body.get("level") else None,
        or_minutes=int(body["or_minutes"]) if body.get("or_minutes") else None,
        min_volume_x=float(body["min_volume_x"]) if body.get("min_volume_x") else None,
        write=bool(body.get("write", True)),
    )
    _record("entry", "%s: %s" % (out.get("display"), out.get("message", out["status"])))
    return out


def api_entry_watchlist(_p, _b):
    return entry_mod.watchlist_check()


def api_plan(_p, body):
    try:
        out = exitplan.plan(
            symbol=body.get("symbol"),
            entry=float(body["entry"]) if body.get("entry") else None,
            stop_rule=body.get("stop_rule"),
            target_rule=body.get("target_rule"),
            or_high=float(body["or_high"]) if body.get("or_high") else None,
            or_low=float(body["or_low"]) if body.get("or_low") else None,
            setup=body.get("setup") or "orb",
            entry_time=body.get("entry_time"),
            entry_bar=int(body["entry_bar"]) if body.get("entry_bar") else None,
        )
    except exitplan.PlanRefused as exc:
        return {"refused": True, "message": str(exc)}
    _record("plan", "%s: entry %.2f, stop %.2f, target %.2f (R:R %.2f)"
            % (out["display"], out["entry"], out["stop"], out["target"], out["reward_risk"]))
    return out


def api_timemachine(_p, body):
    try:
        out = timemachine.run(
            replays=int(body.get("replays") or timemachine.DEFAULT_REPLAYS),
            seed=int(body.get("seed") or timemachine.DEFAULT_SEED),
            until=body.get("until"),
        )
    except (ValueError, data.DataError) as exc:
        return {"refused": True, "message": str(exc)}
    _record("timemachine", out["verdict"])
    return out


def api_size(_p, body):
    try:
        out = budget.size(limit_price=float(body["limit_price"]) if body.get("limit_price") else None)
    except budget.SizeRefused as exc:
        return {"refused": True, "message": str(exc)}
    _record("size", "%d shares, %s, max loss %s (%s)"
            % (out["qty"], store.rupees(out["value"]), store.rupees(out["max_loss"]),
               out["decided_by"]))
    return out


def api_risk(_p, _body):
    out = risk.check()
    _record("risk", "%d passed, %d blocked." % (out["passed"], out["blocked"]))
    return out


def api_tickets(_p, _b):
    return broker.tickets()


def api_approve(_p, body):
    out = broker.approve(int(body["index"]), body.get("reply", ""))
    _record("approve", out["message"])
    return out


def api_approve_basket(_p, body):
    out = broker.approve_basket(body.get("reply", ""))
    _record("approve", out["message"])
    return out


def api_place_basket(_p, _b):
    out = broker.place_basket()
    _record("broker", out["message"])
    return out


def api_place(_p, body):
    out = broker.place(int(body["index"]))
    _record("broker", out["message"])
    return out


def api_flatten(_p, body):
    out = broker.flatten(body["symbol"], int(body["qty"]) if body.get("qty") else None)
    _record("broker", out["message"])
    return out


def api_account(_p, _b):
    return broker.mark_to_market()


def api_history(_p, _b):
    return broker.history()


def api_tick(_p, body):
    out = broker.tick(force=bool(body.get("force")))
    for ev in out.get("events", []):
        _record("fill", "%s %s hit at %.2f: %s %+.2f"
                % (ev["display"], ev["event"], ev["price"], "realised", ev.get("realised") or 0))
    return out


def api_config(_p, body):
    if body:
        cfg = store.save_config(body)
        _record("config", "Settings saved.")
        return cfg
    return store.config()


def api_account_reset(_p, body):
    opening = float(body.get("opening") or store.config()["budget"])
    out = broker.reset_account(opening)
    _record("account", "Fresh paper account %s opened with %s."
            % (out["account"], store.rupees(opening)))
    return out


def api_alerts(_p, _b):
    return alerts.list_rules()


def api_alerts_add(_p, body):
    return alerts.add(body)


def api_alerts_from_plan(_p, _b):
    return alerts.from_plan()


def api_alerts_check(_p, _b):
    out = alerts.check()
    for fired in out.get("fired", []):
        _record("alert", fired["text"])
    return out


def api_alerts_remove(_p, body):
    return alerts.remove(body["id"])


def api_files(_p, _b):
    out = {}
    for name in ("config.json", "regime.json", "candidates.json", "newsfeed.json", "watchlist.json",
                 "movers.json", "news.json",
                 "entry.json", "plan.json", "timemachine.json", "orders.json", "prices.json",
                 "alerts.json", "portfolio.json", "performance.json", "backtest.json",
                 "sweep.json", "state/risk.json", "state/broker.json", "state/alerts.json"):
        value = store.read(name)
        out[name] = {"exists": value is not None,
                     "preview": value if name != "timemachine.json" else
                     (value and {k: value[k] for k in ("verdict", "pool_sessions", "as_of")
                                 if k in value})}
    return out


ROUTES_GET = {
    "/api/state": api_state,
    "/api/clock": api_clock,
    "/api/universe": api_universe,
    "/api/quotes": api_quotes,
    "/api/chart": api_chart,
    "/api/watchlist": api_watchlist,
    "/api/tickets": api_tickets,
    "/api/account": api_account,
    "/api/history": api_history,
    "/api/config": api_config,
    "/api/alerts": api_alerts,
    "/api/files": api_files,
    "/api/entry/watchlist": api_entry_watchlist,
    "/api/movers/saved": api_movers_saved,
    "/api/discipline": api_discipline,
    "/api/watch/saved": api_watch_saved,
    "/api/feed": api_feed,
    "/api/performance": api_performance,
    "/api/backtest/saved": api_backtest_saved,
}

ROUTES_POST = {
    "/api/scan": api_scan,
    "/api/news": api_news,
    "/api/news/watchlist": api_news_watchlist,
    "/api/movers": api_movers,
    "/api/regime": api_regime,
    "/api/backtest": api_backtest,
    "/api/sweep": api_sweep,
    "/api/research": api_research,
    "/api/targets": api_targets,
    "/api/drift": api_drift,
    "/api/taxlots": api_taxlots,
    "/api/longtest": api_longtest,
    "/api/watch": api_watch,
    "/api/feed/poll": api_feed_poll,
    "/api/feed/read": api_feed_read,
    "/api/entry": api_entry,
    "/api/plan": api_plan,
    "/api/timemachine": api_timemachine,
    "/api/size": api_size,
    "/api/risk": api_risk,
    "/api/approve": api_approve,
    "/api/place": api_place,
    "/api/approve-basket": api_approve_basket,
    "/api/place-basket": api_place_basket,
    "/api/flatten": api_flatten,
    "/api/tick": api_tick,
    "/api/config": api_config,
    "/api/account/reset": api_account_reset,
    "/api/alerts/add": api_alerts_add,
    "/api/alerts/from-plan": api_alerts_from_plan,
    "/api/alerts/check": api_alerts_check,
    "/api/alerts/remove": api_alerts_remove,
}

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "NiftyDayTrader/1.0"

    def log_message(self, fmt, *args):  # quieter console
        if "/api/state" in (args[0] if args else ""):
            return
        print("  %s" % (fmt % args))

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, payload, code: int = 200):
        self._send(code, json.dumps(payload, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in STATIC:
            name, ctype = STATIC[path]
            file = WEB / name
            if not file.exists():
                return self._send(404, b"missing " + name.encode(), "text/plain")
            return self._send(200, file.read_bytes(), ctype)
        handler = ROUTES_GET.get(path)
        if not handler:
            return self._json({"error": "unknown endpoint %s" % path}, 404)
        try:
            return self._json(handler(urllib.parse.parse_qs(parsed.query), None))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        handler = ROUTES_POST.get(parsed.path)
        if not handler:
            return self._json({"error": "unknown endpoint %s" % parsed.path}, 404)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._json({"error": "body is not JSON"}, 400)
        try:
            return self._json(handler(urllib.parse.parse_qs(parsed.query), body))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)


def background_loop(stop_event: threading.Event, every: int = 60):
    """While the session runs: walk open brackets, ring alerts, hunt the setup.

    It proposes and never places. An order needs a human to say yes for that
    exact order, and nobody is watching a loop at 11:40 on a Tuesday.
    """
    last_scan = [0.0]
    last_news = [0.0]
    last_regime_day = [""]
    scan_every = 180   # seconds between watcher passes
    news_every = 150   # seconds between news polls, in or out of the session
    while not stop_event.wait(every):
        try:
            # The news feed runs all the time. Results land after the close,
            # regulators publish in the evening, and a Sunday headline is the
            # reason Monday gaps.
            if time.time() - last_news[0] >= news_every:
                last_news[0] = time.time()
                try:
                    feed = newsfeed.poll()
                    for hot in feed.get("hot", []):
                        _record("news", "%s on a name you hold: %s (%s)"
                                % ("/".join(hot["tags"]) or "headline", hot["title"][:110],
                                   hot["source"]))
                except Exception as exc:  # noqa: BLE001
                    _record("error", "news feed: %s: %s" % (type(exc).__name__, exc))

            if not clock.is_open(holidays=store.holidays()):
                continue
            out = broker.tick()
            for ev in out.get("events", []):
                _record("fill", "%s %s at %.2f (auto): realised %+.2f"
                        % (ev["display"], ev["event"], ev["price"], ev.get("realised") or 0))
            for alert in (alerts.check().get("fired") or []):
                _record("alert", alert["text"])

            # One regime read a session, before the entry window opens.
            today = clock.now_ist().strftime("%Y-%m-%d")
            if last_regime_day[0] != today:
                state = regime.assess()
                last_regime_day[0] = today
                _record("regime", "%s, %s (risk %s). %s"
                        % (state["regime"], state["recommendation"], state["risk_level"],
                           "New entries allowed." if state["allows_new_entries"]
                           else "No new entries today unless this changes."))

            if time.time() - last_scan[0] >= scan_every:
                last_scan[0] = time.time()
                found = watcher.scan()
                fresh = [c for c in found.get("candidates", []) if not c["stale"]]
                if fresh and found["gates"]["clear"]:
                    _record("watch", "%s: %s -- entry %.2f, stop %.2f, target %.2f, %d shares, "
                                     "max loss %s. Nothing placed."
                            % (watcher.SETUP_NAME, fresh[0]["display"], fresh[0]["entry"],
                               fresh[0]["stop"], fresh[0]["target"], fresh[0]["qty"],
                               store.rupees(fresh[0]["max_loss"])))
                elif fresh:
                    _record("watch", "%d reclaim(s) found but entries are blocked: %s"
                            % (len(fresh), "; ".join(found["gates"]["blocking"])[:140]))
        except Exception as exc:  # noqa: BLE001
            _record("error", "background: %s: %s" % (type(exc).__name__, exc))


def main():
    parser = argparse.ArgumentParser(description="NIFTY Day Trader (paper trading only)")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-background", action="store_true")
    args = parser.parse_args()

    store.ensure_dirs()
    store.config()
    account = broker.portfolio()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    stop_event = threading.Event()
    if not args.no_background:
        threading.Thread(target=background_loop, args=(stop_event,), daemon=True).start()

    url = "http://%s:%d/" % (args.host, args.port)
    status = clock.status(holidays=store.holidays())
    print("\n  NIFTY Day Trader  --  paper trading only, no real money")
    print("  Account %s, opening balance %s" % (account["account"],
                                                store.rupees(account["opening_balance"])))
    print("  NSE is %s (%s IST, %s)" % (status["state"], status["now_ist"], status["note"]))
    print("  %s\n" % url)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping")
    finally:
        stop_event.set()
        server.shutdown()


if __name__ == "__main__":
    main()
