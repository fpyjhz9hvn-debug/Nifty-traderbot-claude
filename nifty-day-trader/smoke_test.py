"""A live walk down the whole chain, on the paper account. Run it any time:

    python smoke_test.py

It scans, times an entry, plans the exit, replays the day, sizes the order,
risk-checks it and shows the ticket. It stops short of filling anything unless
you pass --place, because a fill needs an explicit yes.
"""
from __future__ import annotations

import argparse
import json

from ndt import (alerts, broker, budget, clock, data, entry as entry_mod, exitplan,
                 news as news_mod, risk, scanner, store, timemachine, universe)


def line(title):
    print("\n" + "=" * 78)
    print("  " + title)
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", help="skip the scan and use this ticker")
    ap.add_argument("--place", action="store_true", help="also approve and fill in the paper account")
    ap.add_argument("--group", default="nifty50")
    args = ap.parse_args()

    line("NSE clock")
    status = clock.status(holidays=store.holidays())
    print(json.dumps(status, indent=2))

    line("Paper account")
    account = broker.mark_to_market()
    print("%s  cash %s  equity %s  open positions %d"
          % (account["account"], store.rupees(account["cash"]),
             store.rupees(account["equity"]), account["open_positions"]))

    symbol = universe.normalise(args.symbol) if args.symbol else None
    if not symbol:
        line("Scanner: opening-range breakouts")
        scan = scanner.scan_orb(group=args.group)
        if scan.get("error"):
            print(scan["error"])
        print("%s of %s charts usable, %s candidates, %s confirmed, %d hit(s)"
              % (scan.get("usable"), scan.get("symbols"), scan.get("candidates"),
                 scan.get("confirmed"), len(scan.get("hits") or [])))
        for hit in (scan.get("hits") or [])[:8]:
            print("   %s  %-12s %10.2f  range %.2f to %.2f  +%.2f%% over"
                  % (hit["trigger_time"], hit["display"], hit["price"], hit["or_low"],
                     hit["or_high"], hit["over_pct"]))
        if not scan.get("hits"):
            line("Scanner: the pre-market scan instead")
            daily = scanner.scan_daily(group=args.group)
            for hit in (daily.get("hits") or [])[:8]:
                print("   %-12s %-16s %10.2f  %s" % (hit["display"], hit["setup"],
                                                     hit["price"], hit["why"]))
            print("\nNo intraday breakout to walk through right now. Pass --symbol SOMETHING "
                  "to run the rest of the chain on a named stock.")
            return
        symbol = scan["hits"][0]["symbol"]

    line("News Monitor: %s" % universe.display(symbol))
    try:
        news = news_mod.headlines(symbol, hours=24, limit=6)
        move = news["move"] or {}
        print("  %.2f, %+.2f%% on the day, %.2f%% range so far"
              % (move.get("price") or 0, move.get("change_pct") or 0, move.get("range_pct") or 0))
        for item in news["headlines"]:
            print("   %s  %-28s %s" % (item["published"] or "        ",
                                       ",".join(item["tags"]) or "-", item["title"][:72]))
        for flag in news["flags"]:
            print("   ! " + flag)
    except Exception as exc:  # noqa: BLE001
        print("  news unavailable:", exc)

    line("Entry Timer: %s" % universe.display(symbol))
    signal = entry_mod.check(symbol, write=True)
    print(signal.get("message"))
    for note in signal.get("notes") or []:
        print("   note:", note)
    if signal["status"] != "triggered":
        print("\nNothing triggered, so the rest of the chain has no entry to work from.")
        return

    line("Exit Planner")
    try:
        plan = exitplan.plan()
    except exitplan.PlanRefused as exc:
        print("REFUSED:", exc)
        return
    print("  Entry        %10.2f at %s (bar %s)" % (plan["entry"], plan["entry_time"], plan["entry_bar"]))
    print("  Stop         %10.2f  %s" % (plan["stop"], plan["stop_why"]))
    print("  Target       %10.2f  %s" % (plan["target"], plan["target_why"]))
    print("  Risk a share %10.2f (%.2f%% of the entry)" % (plan["risk_per_share"], plan["risk_pct"]))
    print("  Reward:risk  %10.2f" % plan["reward_risk"])
    print("  Flat by      %10s IST, win or lose" % plan["flat_by"])

    line("Time Machine")
    try:
        tm = timemachine.run(replays=10000)
        mc = tm["mc"]
        print("  %d replays of the rest of the session to %s (%d bars), seed %d, pool %d sessions"
              % (mc["replays"], mc["until"], mc["bars_replayed"], mc["seed"], tm["pool_sessions"]))
        print("    target first  %5.1f %%" % mc["target_first_pct"])
        print("    stop first    %5.1f %%" % mc["stop_first_pct"])
        print("    neither       %5.1f %%   still open at %s" % (mc["neither_pct"], mc["until"]))
        print("    green         %5.1f %%" % mc["green_pct"])
        print("    price at %s: median %.2f, 90%% between %.2f and %.2f"
              % (mc["until"], mc["median_close"], mc["p5_close"], mc["p95_close"]))
        print("    average R     %+.3f" % mc["avg_r"])
        h = tm["history"]
        print("  History: the same rule on %d past sessions fired %d times: %d target, %d stop, "
              "%d neither. Average R %s"
              % (h["sessions"], h["fired"], h["target"], h["stop"], h["neither"], h["avg_r"]))
        print("  " + tm["verdict"])
        for note in tm["notes"]:
            print("  " + note)
    except (ValueError, data.DataError) as exc:
        print("REFUSED:", exc)

    line("Budget Manager")
    try:
        sized = budget.size()
    except budget.SizeRefused as exc:
        print("REFUSED:", exc)
        return
    for limit in sized["limits"]:
        mark = "  <= decides it" if limit["limit"] == sized["decided_by"] else ""
        print("   %-22s %5d shares   (%s)%s" % (limit["limit"], limit["shares"], limit["math"], mark))
    print("   -> %d shares, %s, max loss %s at the stop"
          % (sized["qty"], store.rupees(sized["value"]), store.rupees(sized["max_loss"])))

    line("Risk Manager")
    checked = risk.check()
    for verdict in checked["verdicts"]:
        print("  %-5s #%d %s %d %s, %s" % (verdict["verdict"], verdict["index"], verdict["side"],
                                           verdict["qty"], verdict["display"], verdict["kind"]))
        print("        entry %.2f (%s), stop %s, target %s: risk %s a share"
              % (verdict["entry_price"], verdict["entry_why"], verdict["stop"],
                 verdict["target"], verdict["risk_per_share"]))
        print("        max loss %s, reward:risk %s" % (store.rupees(verdict["max_loss"]),
                                                       verdict["reward_risk"]))
        for reason in verdict["reasons"]:
            print("        - " + reason)
        for warn in verdict["warnings"]:
            print("        ! " + warn)
    print("  %d passed, %d blocked." % (checked["passed"], checked["blocked"]))

    line("Paper ticket")
    tickets = broker.tickets()
    for ticket in tickets["tickets"]:
        print("  ORDER %d, %s account, built-in paper broker -- %s"
              % (ticket["index"], ticket["account"], ticket["state"]))
        for text in ticket["lines"]:
            print("    " + text)
        print("    " + ticket["why"])

    if args.place and tickets["tickets"]:
        line("Approve and fill (paper)")
        print(broker.approve(1, "yes")["message"])
        print(broker.place(1)["message"])
        print(json.dumps(broker.mark_to_market()["positions"], indent=2))

    line("Alerts")
    print(alerts.from_plan().get("message"))
    print(alerts.check()["message"])

    print("\nNothing real was traded. This is a simulated account.\n")


if __name__ == "__main__":
    main()
