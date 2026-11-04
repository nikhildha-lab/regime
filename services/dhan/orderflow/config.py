"""
All settings in one place. Change numbers here, not inside the code.
"""

# ---------------- Markets ----------------
# signal_symbol : the ETF whose 1-min volume we use to build volume profile + delta
# index_id      : Dhan security id of the index (used for expired-options data)
# lot           : option lot size (your fixed convention)
# strike_step   : gap between option strikes
# expiry_flag   : WEEK for NIFTY (weekly Tuesday), MONTH for BANKNIFTY (monthly only)
MARKETS = {
    "NIFTY": {
        "signal_symbol": "NIFTYBEES",
        "index_id": "13",
        "lot": 65,
        "strike_step": 50,
        "expiry_flag": "WEEK",
    },
    "BANKNIFTY": {
        "signal_symbol": "BANKBEES",
        "index_id": "25",
        "lot": 30,
        "strike_step": 100,
        "expiry_flag": "MONTH",
    },
}

# Dhan "expiryCode" for rolling expired options. 1 = nearest expiry.
# If downloads come back empty, try 0.
EXPIRY_CODE = 1

# Which strikes to download around ATM (ATM-6 ... ATM+6)
OPTION_OFFSETS = range(-6, 7)

# How much history to download
HISTORY_YEARS = 2

# ---------------- Session times (IST) ----------------
MARKET_OPEN = "09:15"
IB_END = "10:15"          # initial balance = first hour
FIRST_ENTRY = "09:20"     # skip first 5 minutes
LAST_ENTRY = "14:45"
FORCE_EXIT = "15:15"

# ---------------- Engine ----------------
SIGNAL_BAR_MIN = 5         # setups look at 5-minute bars
VALUE_AREA_PCT = 0.70      # value area = 70% of volume
PROFILE_BIN_PCT = 0.0002   # price bin size = 0.02% of price

# ---------------- Setup thresholds ----------------
DELTA_RATIO_MIN = 0.10     # bar delta / bar volume needed to count as "real" buying/selling
ABSORB_VOL_MULT = 2.0      # absorption bar volume >= 2x recent average
ABSORB_RANGE_MULT = 0.6    # ...but its range <= 0.6x recent average range
LEVEL_TOUCH_PCT = 0.0005   # "at a level" = within 0.05%
MAX_TRADES_PER_DAY = 2     # per index
MIN_RR = 1.0               # skip trades where target is closer than the stop (reward < 1x risk)

# ---------------- Capital & costs ----------------
CAPITAL = 200_000          # Rs 2L, same as MASTER_BACKTESTER

# NIFTYBEES / BANKBEES MIS (intraday ETF)
BEES_MARGIN = 50_000       # money blocked per BEES trade
BEES_LEVERAGE = 5          # -> Rs 2.5L position
BEES_COST_PCT = 0.0004     # ~0.04% round trip (STT+exchange+stamp+GST), approx
BEES_BROKERAGE = 20        # per order
BEES_SLIPPAGE_TICKS = 1
BEES_TICK = 0.01

# Options (all per leg)
OPT_BROKERAGE = 20         # per order
OPT_STT_SELL_PCT = 0.0015  # 0.15% on sell premium (Budget 2026 rate, from 1 Apr 2026)
OPT_EXCH_PCT = 0.0003503   # exchange txn charge on premium
OPT_STAMP_BUY_PCT = 0.00003
GST_PCT = 0.18
OPT_SLIPPAGE_PTS = 0.5     # points lost per leg each side

# Spread shapes (in strikes from ATM)
DEBIT_SPREAD_WIDTH = 2     # buy ATM, sell 2 strikes OTM
CREDIT_SHORT_OTM = 1       # sell 1 strike OTM
CREDIT_SPREAD_WIDTH = 2    # buy protection 2 strikes further
FAR_HEDGE_WIDTH = 5        # FAR_HEDGE: same sell, protection 5 strikes further (cheap hedge, almost naked)
NAKED_SHORT_OTM = 1        # naked sell 1 strike OTM

# Margin blocked per 1 lot. Lots traded = CAPITAL // margin (at least 1).
LOT_MARGIN = {
    "DEBIT_SPREAD": 50_000,
    "CREDIT_SPREAD": 50_000,
    "FAR_HEDGE": 50_000,
    "NAKED_SELL": 200_000,
}

# Folders
DATA_DIR = "data"
TICKS_DIR = "data/ticks"
REPORT_FILE = "report.html"
