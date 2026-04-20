"""News entity keyword config — edit to tune scoring, no logic here."""

# Each category: weight (float) and keywords (list of lowercase strings).
# Weight scales how much a keyword hit from this category contributes to raw_impact_score.
CATEGORIES = {
    "RBI_POLICY": {
        "weight": 2.5,
        "keywords": [
            "rbi", "reserve bank", "monetary policy", "repo rate", "mpc",
            "interest rate", "crr", "slr", "liquidity", "inflation target",
            "governor das", "governor malhotra", "rate cut", "rate hike",
            "policy rate", "open market operation",
        ],
    },
    "NIFTY_INDEX": {
        "weight": 2.0,
        "keywords": [
            "nifty", "nifty 50", "nifty50", "sensex", "nse", "bse",
            "benchmark", "frontline stocks", "broad market",
        ],
    },
    "DERIVATIVES": {
        "weight": 2.2,
        "keywords": [
            "option", "options", "futures", "f&o", "derivative", "derivatives",
            "put call", "pcr", "call put", "open interest", "oi buildup",
            "implied volatility", "india vix", "vix", "expiry", "rollover",
            "short covering", "long unwinding", "max pain",
        ],
    },
    "MACRO_GLOBAL": {
        "weight": 1.5,
        "keywords": [
            "fed", "federal reserve", "fomc", "powell", "us market", "wall street",
            "dollar", "rupee", "usd inr", "crude oil", "brent", "wti",
            "global selloff", "risk off", "risk on", "fii", "dii",
            "foreign institutional", "emerging market", "capital flows",
        ],
    },
    "BUDGET_FISCAL": {
        "weight": 1.8,
        "keywords": [
            "union budget", "fiscal deficit", "gdp", "gst", "disinvestment",
            "capex", "finance minister", "sitharaman", "economic survey",
            "revenue deficit", "government spending", "direct tax", "indirect tax",
        ],
    },
    "MACRO_DATA": {
        "weight": 1.6,
        "keywords": [
            "cpi", "wpi", "inflation", "iip", "pmi", "trade deficit",
            "current account", "unemployment", "manufacturing output",
            "core inflation", "retail inflation", "wholesale inflation",
        ],
    },
    "CORPORATE": {
        "weight": 1.0,
        "keywords": [
            "earnings", "result", "quarterly", "profit", "revenue",
            "ebitda", "guidance", "management commentary", "promoter",
            "q1", "q2", "q3", "q4", "fy26", "fy25",
        ],
    },
    "GEOPOLITICAL": {
        "weight": 1.3,
        "keywords": [
            "war", "conflict", "sanction", "embargo", "border", "ceasefire",
            "pakistan", "china", "russia", "middle east", "iran",
            "trade war", "tariff", "export ban", "import duty",
        ],
    },
    "REGULATORY": {
        "weight": 1.2,
        "keywords": [
            "sebi", "regulation", "circular", "ban", "suspension", "penalty",
            "compliance", "enforcement", "show cause", "adjudication",
        ],
    },
}

# ── Non-financial / off-topic exclusion ────────────────────────────────────
# Item is dropped (IRRELEVANT, should_store=False) if any of these appear.
# Kept lowercase; matched as substring on combined headline+summary text.
EXCLUDE_KEYWORDS = [
    # Sports
    "cricket", "ipl", "bcci", "virat kohli", "rohit sharma", "dhoni",
    "football", "fifa", "messi", "ronaldo", "premier league", "la liga",
    "tennis", "wimbledon", "grand slam", "olympics", "hockey", "kabaddi",
    "badminton", "f1", "formula 1", "formula one", "chess tournament",
    "match", "wicket", "goal scored", "knockout", "tournament win",
    # Entertainment / celebrity / film
    "bollywood", "hollywood", "tollywood", "kollywood", "box office",
    "movie review", "film review", "trailer", "teaser", "web series",
    "netflix series", "prime video", "hotstar", "ott release",
    "actor", "actress", "celebrity", "superstar", "filmmaker",
    "shah rukh", "salman khan", "aamir khan", "deepika", "ranveer",
    "kardashian", "taylor swift", "beyonce",
    # Lifestyle / human interest
    "recipe", "horoscope", "zodiac", "astrology", "fashion week",
    "beauty tips", "weight loss", "skincare", "hairstyle",
    "wedding", "engagement ceremony", "birthday party",
    "viral video", "meme", "tiktok", "instagram reel",
    # Crime / non-market
    "murder", "rape", "kidnap", "road accident", "plane crash survivors",
    # Recommendations / explainers / listicles (not news)
    "top picks", "top pick", "top 5 ", "top 10 ", "top 3 ", "top 7 ",
    "best stocks", "stocks to buy", "stocks to watch", "picks to buy",
    "recommends buying", "suggests investing", "suggests buying",
    "where to invest", "how to invest", "should you invest", "should you buy",
    "what is ", "what are ", "explained:", "explainer:", "explainer-",
    "guide to", "a guide", "beginner's guide", "beginners guide",
    "everything you need to know", "things to know", "things you need to know",
    "5 things", "10 things", "7 things", "key takeaways from",
    "mutual fund picks", "fund picks", "stock picks", "stock idea",
    "stock tip", "stock tips", "muhurat picks",
]

# ── Context-aware direction inversion ──────────────────────────────────────
# When these context words co-occur with certain direction words, the
# interpretation flips. Each rule: (context_word, direction_word, new_direction)
# Example: "VIX surge" — 'surge' is bullish in isolation but bearish for equity.
INVERSE_CONTEXT = [
    # VIX / volatility — up = bearish, down = bullish for equity
    ("vix", "surge", "bearish"),
    ("vix", "soar", "bearish"),
    ("vix", "spike", "bearish"),
    ("vix", "jump", "bearish"),
    ("vix", "rise", "bearish"),
    ("vix", "climb", "bearish"),
    ("vix", "fall", "bullish"),
    ("vix", "drop", "bullish"),
    ("vix", "decline", "bullish"),
    ("volatility", "surge", "bearish"),
    ("volatility", "spike", "bearish"),
    ("volatility", "ease", "bullish"),
    # Inflation — rise = bearish, fall/cool = bullish
    ("inflation", "surge", "bearish"),
    ("inflation", "soar", "bearish"),
    ("inflation", "rise", "bearish"),
    ("inflation", "jump", "bearish"),
    ("inflation", "climb", "bearish"),
    ("inflation", "fall", "bullish"),
    ("inflation", "drop", "bullish"),
    ("inflation", "ease", "bullish"),
    ("inflation", "cool", "bullish"),
    ("cpi", "rise", "bearish"),
    ("cpi", "jump", "bearish"),
    ("cpi", "fall", "bullish"),
    # Rate hikes = bearish, cuts = bullish (Fed/RBI context)
    ("rate", "hike", "bearish"),
    ("rate", "raise", "bearish"),
    ("rate", "cut", "bullish"),
    ("rate", "reduce", "bullish"),
    ("repo", "hike", "bearish"),
    ("repo", "cut", "bullish"),
    # Bond yields — rise = risk-off = bearish equity
    ("yield", "surge", "bearish"),
    ("yield", "spike", "bearish"),
    ("yield", "jump", "bearish"),
    ("yield", "fall", "bullish"),
    ("yield", "drop", "bullish"),
    # Crude oil — surge = bearish for India (import bill)
    ("crude", "surge", "bearish"),
    ("crude", "soar", "bearish"),
    ("crude", "jump", "bearish"),
    ("crude", "fall", "bullish"),
    ("crude", "drop", "bullish"),
    ("oil", "surge", "bearish"),
    ("oil", "soar", "bearish"),
    # Dollar strength — rupee weakening = bearish
    ("dollar", "surge", "bearish"),
    ("dollar", "soar", "bearish"),
    ("rupee", "fall", "bearish"),
    ("rupee", "plunge", "bearish"),
    ("rupee", "crash", "bearish"),
    ("rupee", "rise", "bullish"),
    ("rupee", "gain", "bullish"),
    # FII outflow = bearish, inflow = bullish (already handled by words but reinforce)
    ("fii", "sell", "bearish"),
    ("fii", "outflow", "bearish"),
    ("fii", "buy", "bullish"),
    ("fii", "inflow", "bullish"),
    # Tariff / trade war — generally bearish
    ("tariff", "impose", "bearish"),
    ("tariff", "raise", "bearish"),
    ("tariff", "hike", "bearish"),
    ("tariff", "cut", "bullish"),
    ("tariff", "remove", "bullish"),
    # War / peace / geopolitics
    ("peace", "doubt", "bearish"),
    ("peace talks", "doubt", "bearish"),
    ("peace", "hope", "bullish"),
    ("war", "end", "bullish"),
    ("war", "ease", "bullish"),
    ("ceasefire", "agree", "bullish"),
    ("ceasefire", "hold", "bullish"),
    ("ceasefire", "collapse", "bearish"),
    ("ceasefire", "break", "bearish"),
    # Commodities where price rise is bearish for importer (India)
    ("natural gas", "rise", "bearish"),
    ("natural gas", "surge", "bearish"),
    ("natural gas", "soar", "bearish"),
    ("natural gas", "jump", "bearish"),
    ("gas price", "rise", "bearish"),
    ("iron ore", "gain", "bullish"),  # China-demand driven; bullish for EM risk-on
    # Supply-chain disruptions
    ("supply", "cut", "bearish"),
    ("supply", "disruption", "bearish"),
    ("strait", "close", "bearish"),
    ("strait", "closes", "bearish"),
    ("hormuz", "close", "bearish"),
]

# Negators — if one of these appears within 2 words BEFORE a direction word,
# the direction word is ignored (treated as neutral).
NEGATORS = [
    "not", "no", "without", "isn't", "aren't", "wasn't", "weren't",
    "won't", "wouldn't", "didn't", "doesn't", "don't", "never",
    "unlikely", "fails to", "failed to",
]

# Direction scoring words — bullish words raise BULLISH score, bearish raise BEARISH
BULLISH_WORDS = [
    "surge", "soar", "rally", "gain", "rise", "jump", "climb", "boost",
    "bull", "bullish", "positive", "optimism", "recovery", "rebound",
    "strong", "outperform", "record high", "upside", "breakout",
    "inflow", "buying", "accumulate", "upgrade",
    "erase losses", "ease", "eases", "cools", "cool off", "restore",
    "peace hope", "hope", "hopes", "ceasefire", "revival", "revive",
]

BEARISH_WORDS = [
    "fall", "drop", "crash", "plunge", "sink", "tumble", "decline",
    "bear", "bearish", "negative", "concern", "risk", "fear", "weak",
    "underperform", "selloff", "sell-off", "pressure", "uncertainty",
    "downside", "breakdown", "outflow", "selling", "downgrade", "caution",
    "loss", "losses", "chaos", "doubt", "in doubt", "worry", "worries",
    "slump", "slumps", "slip", "slips", "struggle", "struggles",
    "disruption", "escalation", "escalates", "tension", "tensions",
]

# Items scoring below this are not stored at all
MIN_STORE_SCORE = 2.0
