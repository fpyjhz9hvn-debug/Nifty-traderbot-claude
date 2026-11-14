"""NIFTY Day Trader -- the day-trader skill chain, ported to NSE, on a paper account.

Modules, in the order one trade walks through them:

    scanner -> entry -> exitplan -> timemachine -> budget -> risk -> broker
    alerts  -- while the trade is open

Nothing here touches real money or a real brokerage account.
"""

__version__ = "1.0.0"
__all__ = ["clock", "universe", "data", "store", "scanner", "entry", "exitplan",
           "timemachine", "budget", "risk", "broker", "alerts"]
