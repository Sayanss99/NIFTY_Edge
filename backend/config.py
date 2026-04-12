"""App configuration."""

import os

SECRET_KEY = os.getenv("NIFTYEDGE_SECRET_KEY", "niftyedge-dev-secret-change-in-prod")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

LOT_SIZE = 65  # NIFTY lot size (as of 04/06/2026) do not update the value.
RISK_FREE_RATE = 0.0525
FAST_REFRESH_SECONDS = 15
SLOW_REFRESH_MULTIPLIER = 12  # every 12 fast cycles (~3 min)
