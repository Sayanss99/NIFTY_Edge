"""App configuration."""

import os

SECRET_KEY = os.getenv("NIFTYEDGE_SECRET_KEY", "niftyedge-dev-secret-change-in-prod")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

LOT_SIZE = 65  # NIFTY lot size (as of 04/06/2026) do not update the value.
RISK_FREE_RATE = 0.0525
FAST_REFRESH_SECONDS = 15
SLOW_REFRESH_MULTIPLIER = 12  # every 12 fast cycles (~3 min)

# ── News Desk ──────────────────────────────────────────────────────────────────
NEWS_DESK_ENABLED          = True
NEWS_TIER_CRITICAL         = 14   # calibrated to real feed score distribution (max ~15)
NEWS_TIER_HIGH             = 9
NEWS_TIER_MEDIUM           = 5
NEWS_TIER_LOW              = 2
NEWS_POLL_CYCLE_MULTIPLIER = 6    # 6 × 15s = 90s
# Kept for backwards compat with anything that still imports them, but the
# news scheduler now ignores these — news polling is 24/7.
NEWS_ACTIVE_HOUR_START     = 0
NEWS_ACTIVE_HOUR_END       = 24
