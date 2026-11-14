"""The NIFTY universe, as Yahoo Finance tickers (NSE symbols carry a .NS suffix).

NIFTY 50 membership changes a few times a year. This list is a snapshot, not a
live feed from NSE, so `validate()` asks Yahoo for each name once and drops the
ones that no longer answer; the app shows how many were kept. You can override
the whole thing with "universe" in trader/config.json.
"""
from __future__ import annotations

INDEX = {"symbol": "^NSEI", "name": "NIFTY 50 index"}
BANK_INDEX = {"symbol": "^NSEBANK", "name": "NIFTY BANK index"}

# NIFTY 50 constituents (snapshot). name is for display only.
NIFTY_50 = [
    ("ADANIENT.NS", "Adani Enterprises"),
    ("ADANIPORTS.NS", "Adani Ports & SEZ"),
    ("APOLLOHOSP.NS", "Apollo Hospitals"),
    ("ASIANPAINT.NS", "Asian Paints"),
    ("AXISBANK.NS", "Axis Bank"),
    ("BAJAJ-AUTO.NS", "Bajaj Auto"),
    ("BAJFINANCE.NS", "Bajaj Finance"),
    ("BAJAJFINSV.NS", "Bajaj Finserv"),
    ("BEL.NS", "Bharat Electronics"),
    ("BHARTIARTL.NS", "Bharti Airtel"),
    ("CIPLA.NS", "Cipla"),
    ("COALINDIA.NS", "Coal India"),
    ("DRREDDY.NS", "Dr Reddy's Laboratories"),
    ("EICHERMOT.NS", "Eicher Motors"),
    ("ETERNAL.NS", "Eternal (Zomato)"),
    ("GRASIM.NS", "Grasim Industries"),
    ("HCLTECH.NS", "HCL Technologies"),
    ("HDFCBANK.NS", "HDFC Bank"),
    ("HDFCLIFE.NS", "HDFC Life Insurance"),
    ("HEROMOTOCO.NS", "Hero MotoCorp"),
    ("HINDALCO.NS", "Hindalco Industries"),
    ("HINDUNILVR.NS", "Hindustan Unilever"),
    ("ICICIBANK.NS", "ICICI Bank"),
    ("INDUSINDBK.NS", "IndusInd Bank"),
    ("INFY.NS", "Infosys"),
    ("ITC.NS", "ITC"),
    ("JIOFIN.NS", "Jio Financial Services"),
    ("JSWSTEEL.NS", "JSW Steel"),
    ("KOTAKBANK.NS", "Kotak Mahindra Bank"),
    ("LT.NS", "Larsen & Toubro"),
    ("M&M.NS", "Mahindra & Mahindra"),
    ("MARUTI.NS", "Maruti Suzuki India"),
    ("NESTLEIND.NS", "Nestle India"),
    ("NTPC.NS", "NTPC"),
    ("ONGC.NS", "Oil & Natural Gas Corp"),
    ("POWERGRID.NS", "Power Grid Corp"),
    ("RELIANCE.NS", "Reliance Industries"),
    ("SBILIFE.NS", "SBI Life Insurance"),
    ("SBIN.NS", "State Bank of India"),
    ("SHRIRAMFIN.NS", "Shriram Finance"),
    ("SUNPHARMA.NS", "Sun Pharmaceutical"),
    ("TATACONSUM.NS", "Tata Consumer Products"),
    # Tata Motors demerged: the commercial-vehicle entity keeps the Tata Motors
    # Limited name under TMCV, the passenger-vehicle one trades as TMPV.
    ("TMCV.NS", "Tata Motors (commercial vehicles)"),
    ("TMPV.NS", "Tata Motors Passenger Vehicles"),
    ("TATASTEEL.NS", "Tata Steel"),
    ("TCS.NS", "Tata Consultancy Services"),
    ("TECHM.NS", "Tech Mahindra"),
    ("TITAN.NS", "Titan Company"),
    ("TRENT.NS", "Trent"),
    ("ULTRACEMCO.NS", "UltraTech Cement"),
    ("WIPRO.NS", "Wipro"),
]

# NIFTY NEXT 50-ish large caps, for breadth when scanning. Best effort.
NIFTY_NEXT_50 = [
    ("ABB.NS", "ABB India"),
    ("ADANIENSOL.NS", "Adani Energy Solutions"),
    ("ADANIGREEN.NS", "Adani Green Energy"),
    ("ADANIPOWER.NS", "Adani Power"),
    ("AMBUJACEM.NS", "Ambuja Cements"),
    ("BAJAJHLDNG.NS", "Bajaj Holdings"),
    ("BANKBARODA.NS", "Bank of Baroda"),
    ("BPCL.NS", "Bharat Petroleum"),
    ("BRITANNIA.NS", "Britannia Industries"),
    ("CANBK.NS", "Canara Bank"),
    ("CGPOWER.NS", "CG Power & Industrial"),
    ("CHOLAFIN.NS", "Cholamandalam Investment"),
    ("DABUR.NS", "Dabur India"),
    ("DIVISLAB.NS", "Divi's Laboratories"),
    ("DLF.NS", "DLF"),
    ("GAIL.NS", "GAIL India"),
    ("GODREJCP.NS", "Godrej Consumer Products"),
    ("HAVELLS.NS", "Havells India"),
    ("HAL.NS", "Hindustan Aeronautics"),
    ("ICICIGI.NS", "ICICI Lombard General"),
    ("ICICIPRULI.NS", "ICICI Prudential Life"),
    ("INDHOTEL.NS", "Indian Hotels"),
    ("INDIGO.NS", "InterGlobe Aviation"),
    ("IOC.NS", "Indian Oil Corp"),
    ("IRFC.NS", "Indian Railway Finance"),
    ("JINDALSTEL.NS", "Jindal Steel & Power"),
    ("JSWENERGY.NS", "JSW Energy"),
    ("LICI.NS", "Life Insurance Corp"),
    ("LODHA.NS", "Macrotech Developers"),
    ("LTTS.NS", "L&T Technology Services"),
    ("MOTHERSON.NS", "Samvardhana Motherson"),
    ("NAUKRI.NS", "Info Edge India"),
    ("PFC.NS", "Power Finance Corp"),
    ("PIDILITIND.NS", "Pidilite Industries"),
    ("PNB.NS", "Punjab National Bank"),
    ("RECLTD.NS", "REC"),
    ("SIEMENS.NS", "Siemens India"),
    ("SHREECEM.NS", "Shree Cement"),
    ("TATAPOWER.NS", "Tata Power"),
    ("TORNTPHARM.NS", "Torrent Pharmaceuticals"),
    ("TVSMOTOR.NS", "TVS Motor"),
    ("UNITDSPR.NS", "United Spirits"),
    ("VBL.NS", "Varun Beverages"),
    ("VEDL.NS", "Vedanta"),
    ("ZYDUSLIFE.NS", "Zydus Lifesciences"),
]

GROUPS = {
    "nifty50": NIFTY_50,
    "next50": NIFTY_NEXT_50,
    "nifty100": NIFTY_50 + NIFTY_NEXT_50,
}


def names() -> dict:
    out = {s: n for s, n in NIFTY_50 + NIFTY_NEXT_50}
    out[INDEX["symbol"]] = INDEX["name"]
    out[BANK_INDEX["symbol"]] = BANK_INDEX["name"]
    return out


def symbols(group: str = "nifty50") -> list:
    return [s for s, _ in GROUPS.get(group, NIFTY_50)]


def display(symbol: str) -> str:
    """RELIANCE.NS -> RELIANCE; ^NSEI -> NIFTY 50."""
    if symbol == INDEX["symbol"]:
        return "NIFTY 50"
    if symbol == BANK_INDEX["symbol"]:
        return "BANK NIFTY"
    return symbol[:-3] if symbol.endswith(".NS") else symbol


def normalise(symbol: str) -> str:
    """Accept RELIANCE, reliance.ns, ^NSEI -> a Yahoo ticker."""
    s = (symbol or "").strip().upper()
    if not s:
        return s
    if s.startswith("^"):
        return s
    if s in ("NIFTY", "NIFTY50", "NIFTY 50"):
        return INDEX["symbol"]
    if s in ("BANKNIFTY", "BANK NIFTY", "NIFTYBANK"):
        return BANK_INDEX["symbol"]
    if s.endswith(".NS"):
        return s
    return s + ".NS"
