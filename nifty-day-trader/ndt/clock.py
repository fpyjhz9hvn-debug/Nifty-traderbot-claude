"""NSE clock. India has no daylight saving, so IST is a fixed UTC+05:30.

Session: 09:15 to 15:30 IST, Monday to Friday. Weekends are closed. Trading
holidays come from trader/config.json ("nse_holidays": ["YYYY-MM-DD", ...]) --
the list ships empty on purpose, because a wrong hard-coded holiday is worse
than no holiday: a day with no bars is reported as "no session data" anyway.
"""
from __future__ import annotations

import datetime as _dt

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30), "IST")

SESSION_OPEN = _dt.time(9, 15)
SESSION_CLOSE = _dt.time(15, 30)
BAR_MINUTES = 5
BARS_PER_SESSION = 75  # 09:15 -> 15:30 in 5-minute bars


def now_ist() -> _dt.datetime:
    return _dt.datetime.now(tz=IST)


def parse_ist(text: str) -> _dt.datetime:
    """"2026-11-13 10:15" or "2026-11-13T10:15" (IST) -> aware datetime."""
    text = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    raise ValueError("could not read %r as an IST date/time" % text)


def parse_hhmm(text: str) -> _dt.time:
    h, m = text.strip().split(":")[:2]
    return _dt.time(int(h), int(m))


def from_epoch(ts: int | float) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(float(ts), tz=IST)


def to_epoch(dt: _dt.datetime) -> int:
    return int(dt.timestamp())


def is_weekend(day: _dt.date) -> bool:
    return day.weekday() >= 5


def is_trading_day(day: _dt.date, holidays=()) -> bool:
    if is_weekend(day):
        return False
    return day.isoformat() not in set(holidays or ())


def session_bounds(day: _dt.date):
    """(open, close) as aware IST datetimes for that date."""
    return (
        _dt.datetime.combine(day, SESSION_OPEN, tzinfo=IST),
        _dt.datetime.combine(day, SESSION_CLOSE, tzinfo=IST),
    )


def is_open(at: _dt.datetime | None = None, holidays=()) -> bool:
    at = at or now_ist()
    at = at.astimezone(IST)
    if not is_trading_day(at.date(), holidays):
        return False
    lo, hi = session_bounds(at.date())
    return lo <= at < hi


def next_open(at: _dt.datetime | None = None, holidays=()) -> _dt.datetime:
    at = (at or now_ist()).astimezone(IST)
    day = at.date()
    lo, _ = session_bounds(day)
    if is_trading_day(day, holidays) and at < lo:
        return lo
    for step in range(1, 15):
        day = at.date() + _dt.timedelta(days=step)
        if is_trading_day(day, holidays):
            return session_bounds(day)[0]
    return lo


def status(at: _dt.datetime | None = None, holidays=()) -> dict:
    at = (at or now_ist()).astimezone(IST)
    lo, hi = session_bounds(at.date())
    trading = is_trading_day(at.date(), holidays)
    if trading and lo <= at < hi:
        state, note = "open", "regular session"
    elif trading and at < lo:
        state, note = "pre-open", "market opens at 09:15 IST"
    elif trading:
        state, note = "closed", "the session ended at 15:30 IST"
    elif is_weekend(at.date()):
        state, note = "closed", "weekend"
    else:
        state, note = "closed", "trading holiday (from your config)"
    nxt = next_open(at, holidays)
    return {
        "state": state,
        "note": note,
        "now_ist": at.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": at.strftime("%A"),
        "session": {"open": "09:15", "close": "15:30", "tz": "IST (UTC+05:30)"},
        "next_open": nxt.strftime("%Y-%m-%d %H:%M"),
        "is_open": state == "open",
    }


def bar_grid(day: _dt.date, minutes: int = BAR_MINUTES):
    """Every bar START time of the session, as aware IST datetimes."""
    lo, hi = session_bounds(day)
    out, t = [], lo
    step = _dt.timedelta(minutes=minutes)
    while t + step <= hi:
        out.append(t)
        t += step
    return out


def bar_close_time(bar_start: _dt.datetime, minutes: int = BAR_MINUTES) -> _dt.datetime:
    return bar_start + _dt.timedelta(minutes=minutes)


def fmt_time(dt: _dt.datetime) -> str:
    return dt.astimezone(IST).strftime("%H:%M")


def fmt_dt(dt: _dt.datetime) -> str:
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M")
