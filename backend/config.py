"""App configuration."""

import os

_DEV_SECRET = "niftyedge-dev-secret-change-in-prod"
SECRET_KEY = os.getenv("NIFTYEDGE_SECRET_KEY", _DEV_SECRET)
if SECRET_KEY == _DEV_SECRET:
    import logging
    logging.getLogger("niftyedge.config").warning(
        "Using the built-in dev JWT secret — set NIFTYEDGE_SECRET_KEY for any "
        "non-local deployment."
    )
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

LOT_SIZE = 65  # NIFTY lot size (as of 04/06/2026) do not update the value.
RISK_FREE_RATE = 0.0525
FAST_REFRESH_SECONDS = int(os.getenv("NIFTYEDGE_FAST_REFRESH_SECONDS", "15"))
SLOW_REFRESH_MULTIPLIER = int(os.getenv("NIFTYEDGE_SLOW_REFRESH_MULTIPLIER", "12"))  # every 12 fast cycles (~3 min)

# ── News Desk ──────────────────────────────────────────────────────────────────
NEWS_DESK_ENABLED          = True
NEWS_TIER_CRITICAL         = 14   # calibrated to real feed score distribution (max ~15)
NEWS_TIER_HIGH             = 9
NEWS_TIER_MEDIUM           = 5
NEWS_TIER_LOW              = 2
NEWS_POLL_CYCLE_MULTIPLIER = 6    # 6 × 15s = 90s
