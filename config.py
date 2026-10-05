# =====================================================================
# DQT alert bot - settings (edit only this file)
# =====================================================================

# Default data source: "yahoo" (no signup, futures data, can be a few minutes late)
#                      "oanda" (free practice account + API token, same feed as OANDA on TradingView)
# Crypto assets always use Yahoo (see "provider" below).
PROVIDER = "yahoo"

# Groups of correlated assets. Inside a group EVERY asset is used as the "chart" asset
# against the other assets of the group (like the Pine indicator with compare symbols).
# "yahoo" = Yahoo Finance ticker, "oanda" = OANDA instrument name.
GROUPS = [
    {
        "name": "Metals",
        "assets": [
            {"name": "XAUUSD", "yahoo": "GC=F", "oanda": "XAU_USD"},
            {"name": "XCUUSD", "yahoo": "HG=F", "oanda": "XCU_USD"},
            {"name": "XAGUSD", "yahoo": "SI=F", "oanda": "XAG_USD"},
        ],
    },
    {
        "name": "Indices",
        "assets": [
            {"name": "NAS100", "yahoo": "NQ=F", "oanda": "NAS100_USD"},
            {"name": "US30",   "yahoo": "YM=F", "oanda": "US30_USD"},
            {"name": "SP500",  "yahoo": "ES=F", "oanda": "SPX500_USD"},
        ],
    },
    {
        "name": "Crypto",
        "assets": [
            {"name": "BTCUSD", "yahoo": "BTC-USD", "provider": "yahoo"},
            {"name": "ETHUSD", "yahoo": "ETH-USD", "provider": "yahoo"},
            # TOTAL market cap: no free intraday source exists, so it is not included.
            # If you get a data source, add it here (needs a new fetch function in dqt_alert.py).
        ],
    },
]

# Cycles to check (Yearly = D bars, Monthly = H4, Weekly = H1, Daily = M15, 90min = M5)
CYCLES = {"Yearly": True, "Monthly": True, "Weekly": True, "Daily": True, "90min": True}

# SMT settings (same as the Pine indicator)
SHOW_NORMAL = True
SHOW_HIDDEN = True
REQUIRE_FVG = True       # False = alert as soon as the SMT is detected

# Fake SMT conditions (same as the Pine indicator)
FAKE_HTF = True          # condition 1
FAKE_FS = True           # condition 2

# Alerts
ALERT_DETECTED = True    # alert 1: SMT detected, FVG has not formed yet
ALERT_FVG = True         # alert 2: SMT + FVG (confirmed)
ALERT_FAKE = True        # also alert fake SMTs (they are marked FAKE in the message)

# Which cycles may alert:
#   True      = always
#   False     = never (the cycle is not even calculated)
#   "aligned" = only when the SMT points the same way as this week's Weekly SMT
#               (the Weekly SMT counts as soon as it is DETECTED, FVG is not needed)
# Weekly must stay enabled for "aligned" to work.
ALERT_CYCLES = {"Yearly": True, "Monthly": True, "Weekly": True, "Daily": "aligned", "90min": False}

# True  = Daily alert only for the FIRST Daily SMT (same direction) after the Weekly SMT
# False = every aligned Daily SMT alerts
DAILY_FIRST_ONLY = True

# Only alert SMTs whose bar closed within the last N minutes
# (protects against old signals if GitHub skips a few runs)
MAX_AGE_MIN = 180
