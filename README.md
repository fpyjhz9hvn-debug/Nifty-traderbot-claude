# NIFTY Day Trader

The [day-trader skill pack](https://github.com/jakeschincariol/day-trader-skill)'s method,
ported from US equities to the **NSE**, wired end to end behind a local web UI, trading a
**simulated paper account**. No brokerage account, no API key, no real money, no order path
out of this machine.

```bash
python app.py
# http://127.0.0.1:8777
```

Python 3.8+. **Nothing to pip install** — standard library only (`http.server` for the app,
`urllib` for the data), same as the skill pack it follows.

---

## What it is

This project implements three Claude skill packs as running software for Indian equities, on a
simulated account:

1. **[day-trader-skill](https://github.com/jakeschincariol/day-trader-skill)** — the intraday
   chain, scan to ticket. The table below.
2. **[claude-trading-skills](https://github.com/tradermonty/claude-trading-skills)** — the
   discipline layer: market regime, drawdown circuit breaker, portfolio heat, expectancy.
3. **[trader-skill](https://github.com/jakeschincariol/trader-skill)** — the position sleeve:
   targets, a rebalance band, tax lots.

They disagree with each other, and the app measures rather than arbitrates: the **Invest** tab
holds the comparison, and on this data the least active approach won.

The first pack is eleven skills that walk one day trade from a scan to a broker ticket, each
step writing a file the next one reads:

| # | Step | Skill it ports | What it does here |
| --- | --- | --- | --- |
| 1 | **Chart Scanner** | `trader-scanner` | Scans the NIFTY 50 / NEXT 50 for opening-range breakouts on 5-minute bars, and a pre-market scan on daily closes (20-day closing-high breakout, bull flag, reclaimed the 20-day average, RSI-14 oversold, oversold bounce) |
| 2 | **News Monitor** | `trader-news` | What is around the name before you time anything: Google News scoped to India plus Yahoo's per-ticker feed, de-duplicated, tagged (results, order win, deal, guidance, regulatory, capital, management, capex) and stamped in IST, next to today's move and range |
| 3 | **Entry Timer** | `trader-entry` | Calls the entry on the first *completed* bar that CLOSES above the opening-range high (or a level you name), with an optional volume check |
| 4 | **Exit Planner** | `trader-exit` | Stop at the opening-range midpoint, the range low, a price, a percent or an ATR multiple; target at 1R/1.5R/2R/3R; flat-by time. Refuses a stop at or above the entry, a target at or below it, or a stop too tight to survive the spread |
| 5 | **Time Machine** | `trader-timemachine` | Replays the rest of the session 10,000 times from the stock's own 5-minute bars over the last 60 days, *and* re-runs the same rule on every past session. Prints the odds, a fan chart, a close histogram and one honest verdict |
| 6 | **Budget Manager** | `trader-budget` | Whole shares inside four limits — max loss per trade, position cap, max order value, cash after the buffer — and says which one decided it |
| 7 | **Risk Manager** | `trader-risk` | PASS or BLOCK per order against every limit, fingerprinted so an edited order has to pass again. An order that only gets you out is never blocked by a day limit |
| 8 | **Paper broker** | `trader-broker` | Three gates, then a simulated fill: the risk check passed *this exact order* today, you typed an explicit yes for it, and you confirmed once more |
| — | **Price Alerts** | `trader-alerts` | The stop, the target and flat-by-the-close in one go, plus above/below/percent-move/time/opening-bell rules, each ringing once |
| — | **Movers** | *new* | Which names actually swing, and how dependably. See below. |
| — | **Regime & edge** | [claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) | The discipline layer: a market-regime gate, a drawdown circuit breaker, portfolio heat, expectancy and a backtest. See below. |

Not ported: `trader-analyst` and `trader-pelosi` read SEC EDGAR's XBRL facts and the US
House Clerk's disclosures. Neither covers Indian listed companies, so rather than fake them
with a different source they are left out.

**The feed runs all the time.** The News step above answers one question about one stock when
you ask it. The **News** tab is the other half: a background poller that keeps a de-duplicated
feed warm every two and a half minutes, **whether or not the market is open** — results land
after the close, regulators publish in the evening, and a Sunday headline is the reason Monday
gaps. Each cycle fetches one market-wide feed plus the next couple of watched names on a
rotating cursor, so eight watched names cost the same few requests a cycle as two; they just
come round less often. It follows whatever you hold, plan, or have just scanned up, and a tag
that matters on a name you hold (results, regulatory, deal, guidance) is pushed to the activity
log. The tab carries an unread badge, and you can filter the feed by name or tag. Like the
watcher, it only runs while `python app.py` is running.

**About the news step.** NSE's own corporate-announcements API answers `403 Forbidden` to
anything that is not a real browser session, so this reads open news feeds instead: Google
News RSS (`hl=en-IN&gl=IN`, searched by company name) and Yahoo's per-ticker headline feed,
which carries some NSE names and not others. Aggregator quote-page spam is filtered out by
title and by source. These are **headlines, not filings** — a reason to look, never a fact
about the company. Anything that matters belongs checked against the company's own filing on
nseindia.com.

## Regime & edge — the discipline layer

Ported from [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills),
whose relevant skills are about *not losing* rather than about picking winners. Four of them
transfer to an NSE intraday long-only system; the rest (FMP/finviz/SEC screeners, options,
futures, COT, dividend and tax work) do not, and are left alone.

| Ported from | What it does here |
| --- | --- |
| `ibd-distribution-day-monitor` | A distribution day is a session that closes ≥0.2% below the previous close **on higher volume**. It stays active for 25 sessions and is cancelled once the index gains 5% from its close. d5/d15/d25 clusters plus the 21 EMA / 50 SMA give a market risk of NORMAL, CAUTION, HIGH or SEVERE |
| `exposure-coach` | Uptrend participation (how many NIFTY names are above their own 50-day and 20-day averages) maps to a regime — Broadening, Concentration, Transitional, Contraction — an exposure ceiling, and a recommendation: NEW_ENTRY_ALLOWED, SELECTIVE, REDUCE_ONLY or CASH_PRIORITY |
| `drawdown-circuit-breaker` | Daily (2%), weekly (5%) and monthly (8%) realised-loss limits, plus a cooldown after consecutive losing exits. Realised damage only — an open position that is merely underwater has not cost anything yet |
| `pre-trade-discipline-gate` + `position-sizer` | Before each entry: written plan, predefined stop, size within plan, no revenge trade inside the window after a loss, circuit breaker clear, regime allowing new entries. Plus **portfolio heat** — total open risk across all positions, capped at 6% — and a **half-Kelly ceiling** on size once there are 20+ closed trades |

The regime gate, the circuit breaker, portfolio heat and the revenge window are enforced by the
**Risk Manager**, so a blocked entry never reaches a ticket. Portfolio heat and half-Kelly are
extra limits in the **Budget Manager**, alongside the original four. Getting out is still never
blocked by any of it.

### Does it work? The backtest says: partly, and the setup itself does not

`python -c "from ndt import backtest; backtest.run()"` replays the opening-range breakout over
every session in the 60-day pool across the universe, with and without the regime gate — same
entries, same exits, so the comparison is fair. On the window to 2026-11-12, 59 sessions and 51
names:

| | Every signal | Only when the regime gate allowed entries |
| --- | --- | --- |
| Trades | 177 over 59 sessions | 21 over 7 sessions |
| Win rate | 34.5% | 38.1% |
| Expectancy | **−0.1048R** a trade | **−0.0025R** a trade |
| Total | −18.54R | −0.05R |
| Worst drawdown | 35.06R | 4.90R |
| Net | −₹21,472 | −₹2,379 |

Read plainly:

1. **The setup as shipped loses money on this window.** −0.10R a trade over 177 trades is not
   noise, and no amount of position sizing fixes a negative expectancy.
2. **The regime gate helps a lot** — it takes expectancy from −0.105R to roughly breakeven and
   cuts the worst drawdown from 35R to 5R — but it does it by *standing aside on 52 of the 59
   sessions*. The gate's value is refusing to trade, which is exactly what the skill claims for
   it, and 21 trades is far too small a sample to call it an edge.
3. **Charges matter more than they look.** Indian intraday charges scale with *turnover*, not
   with risk, so a tight stop — which buys a large position for the same rupee risk — is taxed
   hardest. A replay that sizes only to the risk budget overstates this badly, so
   `backtest.size_like_the_app()` applies the Budget Manager's real position cap and max order
   value; without it the same 177 trades read −₹86,241 instead of −₹21,472.

`backtest.sweep()` runs all eight stop/target combinations, both arms, and reports **every** cell
rather than the best one. On that window: all 8 ungated cells lose; 5 of 16 cells are positive
and all 5 are gated 21-trade samples. One pattern held up across the whole 177-trade sample —
an **`or-low` stop beat `or-mid` in all four target variants** — so the app's default stop is now
`or-low`, with the mechanism stated (a wider stop takes fewer noise stop-outs and buys fewer
shares, cutting the charge drag). That is in-sample evidence from one window; it is reversible in
Settings.

### Seven setups, tested properly

The breakout's 109 stops against 38 targets says this market mean-reverts intraday rather than
trends. `ndt/setups.py` holds seven long-only setups written down *before* anything was run —
four mean-reversion, three trend — and `ndt/research.py` runs them all over the same sessions,
splitting the window: the older 60% of sessions are the **design** half, the newer 40% the
**confirm** half. A setup counts only if it is positive in *both*, on 20+ trades each, and still
positive after charges.

| Setup | Family | Trades | Win | Expectancy | Design | Confirm | Worst DD | Net |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **Opening-range low reclaim** | mean reversion | 176 | 39.8% | **+0.1423R** | +0.2293R | +0.0138R | 9.1R | **+₹3,031** |
| VWAP reclaim | mean reversion | 177 | 45.2% | +0.0356R | +0.0470R | +0.0189R | 13.2R | −₹2,430 |
| ORB, 1.5× volume | trend | 177 | 44.1% | −0.0293R | −0.1333R | +0.1222R | 19.5R | −₹20,470 |
| Gap-down reclaim | mean reversion | 128 | 39.8% | −0.0574R | −0.1726R | +0.1055R | 20.7R | −₹15,808 |
| Opening-range breakout | trend | 177 | 41.2% | −0.0599R | −0.1900R | +0.1298R | 23.7R | −₹17,900 |
| Pullback to the 20-bar EMA | trend | 177 | 31.1% | −0.0981R | −0.0641R | −0.1477R | 45.0R | −₹21,586 |
| First-hour low reclaim | mean reversion | 175 | 27.4% | −0.2195R | −0.2726R | −0.1399R | 41.4R | −₹24,891 |

**One survivor: the opening-range low reclaim** — it loses the opening-range low, then closes
back above it. A failed breakdown rather than a breakout. Positive in both halves, the smallest
drawdown in the bank by a distance (9.1R against the breakout's 23.7R), and the only one with a
positive rupee result after charges. It is now selectable in the Entry Timer, with a
`swing-low` stop (the low since the break).

And the things that keep it honest:

- **14 comparisons were made** (7 setups × gate on/off). One survivor out of 14 is roughly what
  chance alone produces. This is a reason to paper-trade it forward, not to believe it.
- **The confirm half is only +0.0138R** — barely above zero. The strong number is in the design
  half, which is exactly the half that proves nothing.
- **+₹3,031 over 176 trades is ₹17 a trade**, on a ₹5 lakh account over three months. Even taken
  at face value, that is not a living.
- Three trend setups were negative in the design half and positive in the confirm half, which is
  what a regime change looks like, not an edge.

```bash
python -c "from ndt import research; print(research.run()['verdict'])"
```

### The session watch

With the app running during a session, it hunts that surviving setup by itself. Every three
minutes it scans the universe for an opening-range low reclaim, checks every gate the Risk
Manager would check — entry window, circuit breaker, portfolio heat, revenge window, market
regime, trades left today — and lists what it finds on the Dashboard with the entry, the stop,
the target, the share count and the max loss already worked out. It also reads the market
regime once a session, before the entry window opens.

**It proposes and never places.** The broker skill is explicit about why: an order needs a human
to say yes for that exact order, and nobody is watching a background loop at 11:40 on a Tuesday.
A candidate is a shortlist row; `trade ▸` carries it to the trade desk, where the risk check and
your yes still stand between it and a paper fill.

Two stages keep a pass cheap (about 11 seconds for 51 names): spark closes for everything, then
the real chart with highs and lows for the candidates only. The reclaim is defined on closes,
and the lowest close of the opening range is never below its true low, so anything that lost the
real low also lost the proxy — a superset, nothing missed.

The watcher only runs while `python app.py` is running. Close the terminal and the watching
stops; nothing is scheduled outside the app.

```bash
python -c "from ndt import watcher; print(watcher.scan(force=True)['message'])"
```

### On "maximize profit, avoid loss"

The honest version of that instruction, and what is actually built:

- **Avoiding loss is implementable, and it is implemented.** Loss limits, cooldowns, a heat cap,
  a regime gate and a revenge-window block all cap what a bad run costs. The backtest shows the
  regime gate cutting the worst drawdown by a factor of seven on this data.
- **Maximizing profit is not implementable by instruction.** No gate, sizing rule or screen
  creates an edge where the measured expectancy is negative. What the app does instead is
  *measure* it — expectancy, payoff, profit factor, charge drag, max drawdown — and say so when
  it is negative, including on your own paper record.
- On this evidence, trading the shipped breakout live would lose money. The one setup that
  survived a proper split clears about ₹17 a trade before slippage, which is inside the noise.
  The right use of all this is to keep paper-trading, let the sample grow, and watch whether
  expectancy stays positive on 30+ live paper trades. The app will tell you either way rather
  than flatter the result.

## Invest — the position sleeve, and the finding that matters most

Ported from [jakeschincariol/trader-skill](https://github.com/jakeschincariol/trader-skill),
the v1 pack. It is not a day-trading pack: its model is targets, a rebalance band and tax lots
— own things, trade rarely. Three of its skills transfer; `trader-copycat` (13F filings) and
`trader-analyst` (SEC XBRL) do not, because neither covers Indian companies.

| Ported from | What it does here |
| --- | --- |
| `trader-screener` | Rules against numbers, showing every pass and fail with the number and the threshold. The v1 rules are fundamental and come from SEC XBRL; free data carries no Indian fundamentals, so these screens run on trend and momentum only. That is a real limit, stated rather than papered over |
| `trader-budget` (weights mode) | Cash buffer off the top, weights renormalised, anything over the position cap pinned and its excess shared out, so a capped name is not bought back over the cap every month |
| `trader-rebalance` | Value everything, compute drift in percentage points, trade only what is outside the band (default 5), sells before buys, skip tiny trades, scale buys to the cash above the buffer |
| `trader-tax` | FIFO cost-basis lots from the paper account's own fills, unrealised gain per lot, holding period, harvest candidates. The US one-year line becomes **12 months**, and India's lack of a US-style wash-sale rule is reported as information rather than used to disallow anything. No tax rate is named anywhere |

Rebalance orders carry `segment: delivery`, so they are charged as holdings (no brokerage, STT
on both sides, higher stamp duty) and the Risk Manager drops the rules that exist only because
a day trade must be flat by the close — the entry window, the required stop, reward:risk, the
per-day trade count. Size, cash, concentration and the circuit breaker still apply, and the
paper broker still needs your yes.

### Holding vs day trading, measured

`ndt/longtest.py` runs three arms over the same daily window, same universe, same money, same
charge model. 147 sessions to 2026-11-13:

| Arm | Return | A year | Worst drawdown | Trades | Charges | Charges / account | Turnover |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| NIFTY 50 buy and hold *(benchmark)* | **+5.46%** | +9.50% | 10.26% | 0 | ₹0 | 0.00% | 0× |
| Equal-weight NIFTY 50, held | +3.51% | +6.06% | 8.46% | 48 | ₹461 | 0.09% | 0.78× |
| Momentum top 10, monthly rebalance | −3.18% | −5.36% | 14.34% | 74 | ₹2,895 | 0.58% | 5.15× |
| *Intraday ORB, 60 sessions (for contrast)* | *−4.29%* | — | — | *177* | *₹16,069* | *3.21%* | *~35×* |

Three things fall out, and they point the same way:

1. **Doing nothing won.** Holding the index beat both active arms and the intraday arm. That is
   the usual result in this kind of test and the one worth taking seriously.
2. **Cost tracks activity, monotonically**: 0.00% → 0.09% → 0.58% → 3.21% of the account, in
   exact order of turnover. The intraday arm paid 35× its account value in turnover to lose
   money.
3. **Momentum was the worst active arm** — lower return *and* the deepest drawdown — and it made
   −4.51% in the first half against +1.51% in the second. A result that lives in one half is a
   window, not an edge.

So the honest answer to "make the trades profitable" is not a better setup. On everything
measured here it is **trade less**: the only arm that made money relative to its costs is the
one that barely traded. The momentum screen and rebalancer are built and usable, but the app's
own measurement says the plain benchmark beat them, and the README is not going to pretend
otherwise.

### Buying the sleeve: basket approval

A 44-name equal-weight basket needs 44 approvals under the packs' rule of one yes per order and
never "yes to all". That rule is right where a yes can reach a real broker. It cannot here —
this account is a ledger in a JSON file with no order path out of the machine — so the Invest
tab has **Risk-check and approve the basket**: one risk check over every proposal, then one yes
covering the orders that passed.

It is honest about what it is. The approval is written to the log as a **basket**, naming every
order it covers by fingerprint, so the record never pretends you said yes forty-four times.
Change any order and its fingerprint changes, and the approval stops covering it. A BLOCKED
order never joins a basket.

The quantities are computed from the prices at the moment the proposals are written, so a basket
written on Friday cannot be filled on Monday as-is. The sequence on a trading day is:

1. **Build targets** (or keep the saved ones)
2. **Write the proposals** — re-prices and re-sizes everything at today's prices
3. **Risk-check and approve the basket** — one yes, then it fills in the paper account

Two constraints worth knowing. Whole shares only, so at a ₹4,405-per-name sleeve the seven most
expensive NIFTY names (MARUTI at ₹12,854, ULTRACEMCO, BAJAJ-AUTO, APOLLOHOSP, EICHERMOT,
HEROMOTOCO, TITAN) round to zero shares and drop out — equal weight across 44, not 51. And the
v1 pack's 5-point rebalance band assumes a handful of large positions; at 51 names of 0.9 % each
nothing ever drifts 5 points and the rebalancer writes nothing, so the effective band is the
smaller of the points band and 25 % of the name's own target (`rebalance_relative_band_pct`).

```bash
python -c "from ndt import longtest; print(longtest.run()['verdict'])"
python -c "from ndt import invest; print(len(invest.build_targets()['targets']), 'targets')"
python -c "from ndt import invest; print(invest.drift()['message'])"
python -c "from ndt import taxlots; print(taxlots.report()['totals'])"
```

## Movers — which names actually swing

A day trade needs room to move: a stock whose whole session fits inside 0.6 % cannot pay for
a stop, a spread and the charges, however clean the setup looks. The **Movers** tab ranks the
universe on what its own last 20 sessions of 5-minute bars did:

| column | what it means |
| --- | --- |
| **day range** | mean (session high − session low) / session open, in percent |
| **steadiness** | 1 − stdev(range)/mean(range). High means the range turns up day after day, not from one wild session |
| **swing** | 1 − efficiency ratio: net move divided by distance actually travelled, flipped. High means it goes up and down and ends near where it started; low means it trends |
| **turns/day** | how often the 5-minute closes change direction |
| **per 5-min bar** | mean absolute 5-minute move, in percent |
| **ORB fired / target / stop** | the opening-range breakout re-run on those same sessions with an or-mid stop and a 2R target |

Score = `day range × (0.5 + 0.5 × steadiness) × (0.5 + 0.5 × swing)` — nothing cleverer than
"it moves, both ways, and it keeps doing it". Sort by the score, the day range, turnover, or
the setup's own target rate.

**On "predictable": no screen makes a stock predictable, and this one does not claim to.** A
high score says only that there has been room to work with. That is why the setup's real
record on those very sessions sits in the same row — on a recent run the widest-swinging name
in the NIFTY 50 had taken the 2R target 2 times out of 15, while a mid-ranked one had taken it
8 times out of 11. Read those two columns before the score.

The first ranking pulls 60 days of bars for every name and takes about 35 seconds for the
NIFTY 50; after that it is cached for half an hour, and "Show the last ranking" is instant.

## What is different because it is India, not the US

| | US pack | here |
| --- | --- | --- |
| Session | 09:30–16:00 New York, DST worked out in code | 09:15–15:30 IST, a fixed UTC+05:30, no DST |
| Bars in a session | 78 five-minute bars | 75 five-minute bars |
| Opening range | 09:30, 09:35, 09:40 | 09:15, 09:20, 09:25 |
| Default windows | no entries before 09:45 / after 15:30, flat by 15:55 | before 09:30 / after 15:00, flat by 15:20 |
| Universe | ~5,900 names from Nasdaq Trader's symbol directories | NIFTY 50 (+ NEXT 50), Yahoo `.NS` tickers |
| Money | dollars | rupees, Indian digit grouping (₹1,24,488.90) |
| Costs | not modelled | estimated NSE intraday charges come out of the paper balance: brokerage (0.03% capped at ₹20), STT 0.025% on sells, exchange 0.00297%, SEBI 0.0001%, stamp duty 0.003% on buys, 18% GST on the fee parts. All editable in Settings, or switch them off |
| Broker | Alpaca's MCP server, paper account | a built-in simulator. There is no connector to switch on |
| Holidays | NYSE calendar hard-coded | `nse_holidays` in the config, empty by default — see the note below |

**Trading holidays.** NSE's holiday list is not hard-coded, because a wrong hard-coded date
is worse than none: weekends are handled, and a day with no bars reports itself as having no
session data. Put the year's dates into `trader/config.json` as
`"nse_holidays": ["2027-01-26", ...]` if you want the clock to know them in advance.

**The NIFTY 50 list is a snapshot**, not a live feed from NSE. Membership changes a few times
a year; a name that stops answering is simply dropped from a scan and counted in "failed".
It was checked against Yahoo on 2026-11-13 and two tickers had gone: Tata Motors is now two
listings after its demerger (`TMCV` for the commercial-vehicle entity, `TMPV` for passenger
vehicles) and `LTIM` no longer answers. Re-check it yourself with:

```bash
python -c "from ndt import data, universe; print(data.validate(universe.symbols('nifty100'))['dead'])"
```

## The data

Free, delayed, unofficial Yahoo Finance endpoints — the same sources the skill pack uses,
pointed at `.NS` tickers and `^NSEI`:

- `/v8/finance/chart/<TICKER>` — 5-minute bars, today or 60 days back
- `/v7/finance/spark` — closes for 20 tickers a request, for the scans

They can lag, rate-limit or change shape. Requests go to `query2` first and fall back to
`query1`, 0.25 s apart, cached briefly. A bar counts only once it has closed; Yahoo's live
in-progress tick is never treated as a bar.

## The paper account

`trader/portfolio.json` holds an account number, cash, positions, every trade and an equity
curve. A market order fills at the latest delayed quote; charges come out of the balance.
While the session runs, a background loop every 60 seconds walks each open position against
the newest bars exactly as a bracket order would: **the stop is checked before the target**,
a bar that touches both counts as the stop, and anything still open at your flat-by time is
closed at the market. Nothing holds overnight.

Honest about its own limits: a stop fills **at** the stop here, and a real one can fill far
worse in a fast market or across a gap. There is no slippage, no queue, no partial fill and
no market impact. A fill is also refused outright if the live price has already fallen to or
below the plan's stop — a trigger from two hours ago is a different trade.

## The files, step by step

Everything lands in `trader/`, each step reading what the one before it wrote — the skill
pack's own layout, visible in Settings → "The chain's files":

```
config.json      your rules            orders.json      the proposed bracket order
regime.json      the market regime     portfolio.json   the paper account
candidates.json  the session watch's shortlist
newsfeed.json    the rolling news feed
targets.json     the position sleeve's target weights
taxlots.json     cost-basis lots and holding periods
longtest.json    holding vs day trading
watchlist.json   the scan's hits       prices.json      the latest quotes
movers.json      the swing ranking     alerts.json      the alert rules
news.json        the last headlines    performance.json expectancy on your own record
entry.json       the trigger           backtest.json    the 60-day replay
plan.json        stop, target, R       sweep.json       the whole parameter grid
                                       research.json    the seven-setup test
timemachine.json the odds              state/risk.json    the verdicts, fingerprinted
                                       state/broker.json  every yes, fill and exit
                                       state/alerts.json  what already rang
```

## Also runnable from the command line

```bash
python smoke_test.py                 # scan, then walk the whole chain on the first hit
python smoke_test.py --symbol INFY   # skip the scan
python smoke_test.py --symbol INFY --place   # also approve and fill, on paper

python -c "from ndt import regime; print(regime.assess()['rationale'])"
python -c "from ndt import discipline; print(discipline.circuit_breaker()['rationale'])"
python -c "from ndt import performance; print(performance.report()['verdict'])"
python -c "from ndt import backtest; print(backtest.run()['verdict'])"
python -c "from ndt import backtest; print(backtest.sweep()['verdict'])"
```

## Fine print

**Not financial advice.** This is software that reads free public data and does arithmetic.
It does not know your situation and it cannot see the future. Most people who day trade lose
money. Decide for yourself, and talk to someone licensed if you need advice.

**Paper only, by construction.** Nothing in this repository can place a real order. There is
no brokerage connector, no API key and no live mode to turn on. If you ever wire one in, the
three gates and the Risk Manager are the part to keep.

**A simulation is not a prediction.** The Time Machine is built from 60 days of this stock's
own 5-minute bars, with a fixed seed (19) so any run repeats exactly. Results, a halt, a
policy change or a headline can do what those 60 days never did.

**Costs are estimates.** The charge model is a discount-broker shape as of writing. Rates
change and your broker's will differ. Edit them in Settings.

**No part of this makes money appear.** The discipline layer caps what a losing run costs; it
does not create an edge. On the only window it has been measured over, the shipped setup has
negative expectancy, and the README says so above rather than burying it. A backtest is one
window, in-sample, with optimistic fills — treat every number in it as the best case.
