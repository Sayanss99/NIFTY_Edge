# NiftyEdge — NIFTY Options Trading Dashboard

A real-time web app for NIFTY options traders to monitor positions, track risk, get hedge suggestions, and spot directional opportunities — all in one place.

---

## Table of Contents

1. [Project Structure](#project-structure)
2. [Tech Stack](#tech-stack)
3. [Getting Started](#getting-started)
4. [Dashboard](#dashboard)
5. [Option Chain](#option-chain)
6. [Positions](#positions)
7. [Trade Journal](#trade-journal)
8. [Analytics](#analytics)
9. [Strategy Builder](#strategy-builder)
10. [Trade Suggestion Engine](#trade-suggestion-engine)
11. [Alerts — Setup Guide](#alerts--setup-guide)
12. [News Desk](#news-desk)
13. [Risk Scoring — How It Works](#risk-scoring--how-it-works)
14. [Hedge Suggestions](#hedge-suggestions)
15. [Directional Signal](#directional-signal)
16. [Support & Resistance Levels](#support--resistance-levels)
17. [Settings & Preferences](#settings--preferences)
18. [Running the App](#running-the-app)

---

## Project Structure

```
NIFTY_Edge/
├── backend/
│   ├── main.py                  # FastAPI app entry point, static file serving, route registration
│   ├── models.py                # SQLAlchemy ORM models (User, Trade, Alert, etc.)
│   ├── database.py              # SQLite DB setup via SQLAlchemy
│   ├── auth.py                  # JWT auth, password hashing (bcrypt), token validation
│   ├── config.py                # App-wide constants (lot size, risk thresholds, etc.)
│   ├── market_data.py           # NSE data fetching: spot, VIX, option chain, expiries
│   ├── scheduler.py             # APScheduler jobs: market updates, signal capture, suggestion sanity checks
│   ├── ws_manager.py            # WebSocket connection manager (broadcasts to all connected clients)
│   ├── alert_engine.py          # Alert evaluation logic (runs on every market update)
│   ├── trade_suggestion_engine.py # Auto buy-trade suggestion logic
│   ├── routes_auth.py           # /api/auth/* — login, signup, settings
│   ├── routes_trades.py         # /api/trades/* — add, close, list trades
│   ├── routes_market.py         # /api/market/* — snapshot, expiries, option chain, S/R, risk, hedge
│   ├── routes_analytics.py      # /api/analytics/* — P&L curves, heatmap, strike patterns, signal history
│   ├── routes_alerts.py         # /api/alerts/* — CRUD alerts, alert history, unread count
│   ├── routes_strategies.py     # /api/strategies/* — strategy CRUD, payoff computation
│   ├── routes_suggestions.py    # /api/suggestions/* — list, execute, reject suggestions
│   ├── routes_news.py           # /api/news/* — feed, ticker, recent-context, dismiss, stats
│   ├── news_entities.py         # Keyword categories, bullish/bearish word lists, MIN_STORE_SCORE config
│   ├── news_sources.py          # RSS feed source registry (name, url, enabled, weight)
│   ├── news_scorer.py           # Rule-based scorer: score_news_item() → NewsScore dataclass
│   ├── news_scraper.py          # fetch_and_store_news() — RSS fetch, dedup, score, persist
│   └── niftyedge.db             # SQLite database file (auto-created on first run)
│
└── frontend/
    ├── index.html               # Root redirect — sends unauthenticated users to /login
    ├── css/styles.css           # All app styles: layout, dark mode, cards, components
    ├── js/
    │   ├── api.js               # API client — fetch wrapper, auth header injection, getUser(), logout()
    │   ├── utils.js             # Shared UI utilities: buildHeader(), buildSidebar(), tour, profile card, formatters
    │   └── ws.js                # WebSocket client with reconnect logic and event emitter
    └── pages/
        ├── login.html           # Animated landing + login/signup page
        ├── dashboard.html       # Main dashboard (chart, metrics, positions summary, risk meter)
        ├── option_chain.html    # Live option chain table with strike lookup
        ├── positions.html       # Open positions with risk scores and hedge suggestions
        ├── journal.html         # Closed trade history with filters and CSV export
        ├── analytics.html       # Performance charts, heatmap, signal accuracy history
        ├── strategy_builder.html# Multi-leg strategy builder with payoff diagram
        ├── alerts.html          # Alert management and trade signals
        └── news_desk.html       # News Desk — live feed with scoring and filters
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Python 3.11+, FastAPI, Uvicorn |
| **Database** | SQLite via SQLAlchemy ORM |
| **Auth** | JWT (PyJWT), bcrypt password hashing |
| **Data** | NSE via `pnsea` library + `yfinance` for IV history |
| **Scheduler** | APScheduler (background jobs during market hours) |
| **Frontend** | Vanilla HTML/CSS/JS — no framework |
| **Realtime** | WebSocket (FastAPI WebSocket + custom `ws.js` client) |
| **Charts** | HTML5 Canvas (custom drawn — no chart library) |
| **News** | `feedparser` (RSS) + `gnews` fallback; rule-based scorer (no ML) |

---

## Getting Started

### First-Time Setup

1. Open the app in your browser (default: `http://localhost:8000`)
2. The landing page has an animated NIFTY chart background and floating market metric badges
3. Click the **Create account** tab
4. Enter your name, email, password (minimum 6 characters), and confirm the password — a live ✓/✗ indicator shows whether the passwords match
5. You'll be logged in automatically and taken to the Dashboard
6. A **first-time onboarding tour** will appear automatically (see [Onboarding Tour](#onboarding-tour) below)

### Logging In

- Enter your email and password on the login page
- You stay logged in for 7 days — no need to log in every time
- Click your name/avatar in the top-right header to open the **profile card** (see [Profile Card](#profile-card))

### Onboarding Tour

The first time you log in with a new account, a guided tooltip tour appears automatically:

1. **Select Expiry** — highlights the expiry dropdown and explains what it controls
2. **Navigate Modules** — highlights the sidebar and describes each page
3. **Live Market Analytics** — highlights the metrics strip
4. **Price & IV Alerts** — highlights the bell icon

The tour shows only once per account (keyed by email in localStorage) and never again. You can skip it at any time with the "Skip all" button.

---

## Dashboard

The Dashboard is your command center. Everything updates automatically in real-time when the market is open.

### Market Overview Strip

At the top, you'll see these key numbers:

| Card | What It Shows |
|------|--------------|
| **Nifty Spot** | Current NIFTY index price with today's change |
| **India VIX** | Fear gauge — higher VIX = more volatility expected |
| **Days to Expiry** | Trading days left until the selected expiry |
| **Calc Range** | Expected price range for NIFTY based on VIX (1 standard deviation) |
| **Tendency** | Simple market mood — Positive, Negative, or Neutral (based on option skew + VIX) |
| **Max Pain** | The strike where option writers make the most money — NIFTY tends to gravitate here near expiry |
| **Directional** | Advanced 8-factor signal — BULLISH, BEARISH, or NEUTRAL with how many factors agree. Also shows a news annotation ("News: N HIGH · X↑ Y↓") if any HIGH/CRITICAL news items landed in the last 2 hours — purely cosmetic, does not affect the signal calculation |
| **IV Rank** | Where current ATM IV sits in its 52-week range (0% = historical low, 100% = historical high). Hover the card for full detail |

### News Ticker Strip

A scrolling ticker strip appears between the metrics strip and the main grid whenever there are HIGH or CRITICAL news items from the last 4 hours. Each item shows:
- A colored dot (red = CRITICAL, amber = HIGH)
- The headline
- Source name and how long ago it was fetched

Hovering the strip pauses the scroll. The ticker is automatically updated by the WebSocket `news_alert` event whenever the scheduler fetches new items.

### Range & Spot Monitor Chart

A visual chart showing:
- **Blue band** = Calculated Range (1 standard deviation move)
- **Amber band** = VIX Range (wider, based on your VIX Offset setting)
- **Red diamond** = Current NIFTY spot price
- **Green/red dashed lines** = Pivot S/R levels
- **Purple dashed line** = Max Pain level
- **Dark red/navy lines** = Your open CE/PE positions

Toggle layers using the legend chips below the chart. Labels are auto-deconflicted so they never overlap.

**VIX Offset %** (top-right of chart): Adjusts how wide the amber range band is. Default is 50%. Increase for a wider safety margin. **Y Zoom** slider adjusts the vertical scale.

### Support & Resistance

Shows the strongest price levels where NIFTY is likely to pause or reverse:

- **OI + Fibonacci**: Levels where high Open Interest coincides with Fibonacci retracement — most reliable
- **Pivot + OI**: Standard pivot points ranked by how much Open Interest backs them up
- Supports are in green, Resistances in red
- Levels with a blue glow border mean OI and Fibonacci agree on that level (high confidence)

### 25-Delta Risk Reversal

Compares the cost of call options vs put options:
- **Negative RR** (e.g., -5.0) = puts are expensive = market is fearful = Bearish skew
- **Positive RR** (e.g., +3.0) = calls are expensive = market expects upside = Bullish skew
- **Near zero** = no strong bias = Neutral

### Risk Meter

Shows how risky your current open positions are:
- **CE Risk**: Risk score for your call-side positions (0-100)
- **PE Risk**: Risk score for your put-side positions (0-100)
- Color goes from green (safe) to red (dangerous)
- Below the bars, a table breaks down risk by 6 factors (Delta, Gamma/DTE, IV, Premium, Proximity, Theta Ratio)
- If hedge is needed, a red alert box appears saying **"HEDGE REQUIRED"**

### Open Positions Summary

Quick view of your top 4 open positions with IV change, delta, and P&L. Click **View All** to go to the full Positions page.

---

## Option Chain

A live view of the NIFTY option chain with all strikes.

### How to Use

1. Select an expiry from the top-right dropdown
2. The table shows all strikes with CE data on the left and PE data on the right
3. The ATM (at-the-money) strike is highlighted in yellow
4. Click any strike row to look up its details in the CE/PE lookup cards at the top

### Strike Lookup

Two cards at the top let you look up any CE or PE strike:
- See LTP, Delta, IV, Gamma, Theta, OI, Volume
- See **Premium Ranking** — tells you if the premium is:
  - **Very Low** — cheap premium, far OTM
  - **Sweet** — good value zone for selling
  - **Slightly Higher** — paying a bit more
  - **High** — expensive premium

### Adding a Trade from the Option Chain

Each strike row in the chain has two buttons:
- **↓ Sell** — immediately adds the strike as a SELL trade and navigates to the Positions page
- **↑ Buy** — immediately adds the strike as a BUY trade and navigates to the Positions page

There is no separate "Add" button — clicking Sell or Buy does everything in one step.

---

## Positions

### Adding a Trade

1. Click **+ Add Trade**
2. Choose type: CE SELL, PE SELL, CE BUY (hedge), or PE BUY (hedge)
3. Enter Expiry (format: `17-Apr-2026`), Strike, Entry Premium, and Quantity (in lots)
4. For hedge trades (BUY types), you can link it to an existing SELL position
5. Click **Add Trade**

### Position Cards

Each position shows:
- Trade type badge, strike price, and quantity
- Entry IV vs Current IV (with % change)
- Entry Delta vs Current Delta
- Entry Premium vs Current LTP
- **Risk Score badge** — shows the computed risk (OK / WARNING / DANGER / CRITICAL)
- **Unrealized P&L** with a visual bar

### When Hedge is Needed

If a position's risk score crosses 65 or a hard trigger fires, you'll see:
- A pulsing **"HEDGE REQ"** or **"HARD TRIGGER"** badge
- A **Hedge Suggestion** box below the position showing:
  - Recommended strike and price to buy
  - Number of lots to buy
  - Total cost
  - What your P&L would be if things go wrong (Worst P&L)
  - What your P&L would be if things stay calm (Best P&L)
  - A confidence score (higher = more reliable suggestion)
  - A short rationale explaining why this strike was chosen

### Closing a Trade

1. Click **Close** on any open position
2. Choose current LTP (auto-filled) or enter a manual exit price
3. Choose how many lots to close (can do partial close)
4. See the P&L preview before confirming
5. Click **Close Position**

---

## Trade Journal

Your trading history and performance record.

### Summary Cards

- **Total P&L** — cumulative realized profit/loss across all closed trades
- **This Month** — P&L for the current calendar month only
- **Win Rate** — percentage of trades that were profitable
- **Avg P&L** — average profit/loss per trade

### Viewing Closed Trades

- **Flat View**: A sortable table of all closed trades. Click column headers to sort
- **Grouped View**: Trades organized by expiry with subtotals

### Filters

Use the dropdowns to filter by:
- Specific expiry
- Trade type (CE/PE, SELL/BUY)
- Result (Profit only, Loss only, or All)

### Export to CSV

Click **Export CSV** to download your full trade history as a spreadsheet file. Includes: exit date, type, strike, expiry, entry/exit prices, quantity, P&L, IV, delta, duration.

---

## Analytics

### Performance Tab

- **Cumulative P&L curve** — visual chart of how your P&L grew over time
- **P&L by Expiry** — bar chart comparing performance across different expiries
- **Monthly heatmap** — calendar-style view of your monthly returns
- Time filters: All time, 1 month, 3 months, 6 months

### Strike Patterns Tab

- Shows which strike distances work best for you (how far OTM you typically sell)
- **Win Rate by VIX** — shows if you do better in low, medium, or high volatility environments
- Insights panel with suggestions based on your trading patterns

### Historical Insights Tab

Tracks how accurate the app's signals are over time by capturing a daily snapshot at market close.

- **Summary cards:** Total signals, bullish accuracy %, bearish accuracy %, tendency agreement %
- **Signal chart:** Dual-axis chart with directional score (line) vs next-day Nifty change % (bars). Dots colored green (correct) or red (incorrect)
- **Signal table:** Full history with date, close, VIX, RR, tendency, direction, score, next-day change, and correct/wrong badge
- The system automatically captures signals at ~3:30 PM each day and backfills the previous day's actual Nifty performance the next day

---

## Strategy Builder

The Strategy Builder lets you compose multi-leg NIFTY option strategies and instantly see the payoff diagram at expiry.

### Analytics Bar (top of page)

A compact analytics panel split 60/40:

- **Left (60%) — 18 metric tiles** across two rows: IV Rank, Skew, PCR, OI Buildup, OI Change, GEX, VIX Level, VIX Trend, Risk Reversal, Calc Range, Tendency, Directional, Max Pain, DTE, Theta/Vega, Premium Rank, Term Structure, and Spot
- **Right (40%) — Market Outlook**: 2-3 sentence AI-generated summary of the current market state with a colour-coded signal badge (Bullish / Bearish / Cautious / Neutral) and an "✦ AI validated" label

### Building a Strategy

1. Select an expiry from the dropdown at the top of the builder
2. Use the **leg wizard** — choose CE or PE, SELL or BUY, enter a strike and premium
3. Add up to 4 legs (e.g., a strangle = 1 CE SELL + 1 PE SELL)
4. The **payoff chart** (Canvas) updates live as you add/remove legs — shows P&L across a spot price range at expiry
5. Hover the payoff chart to see exact P&L at any spot level
6. Click **▶ Execute** to create the strategy — all legs are saved as linked trades in your Positions page

### Payoff Chart

- X-axis: NIFTY spot price at expiry
- Y-axis: Net P&L in ₹
- Green area = profit zone, red area = loss zone
- Current spot is marked with a vertical dashed line

---

## Trade Suggestion Engine

The app can auto-generate **buy trade suggestions** when the directional signal and tendency agree. These are designed to be highly selective because buy trades are inherently risky.

### Two Types of Suggestions

| Type | When | What Happens |
|------|------|-------------|
| **Overnight** | ~3:15 PM | If 6+ factors agree with score >= 2.0 and tendency confirms, a buy suggestion is created. Sanity-checked at ~3:25 PM — if signal weakens, it's auto-rejected. SL = 70% of entry, Target = 150% |
| **Intraday** | ~10:00 AM | Stricter: 6+ factors, score >= 2.5, confidence >= 65%. Sanity-checked every 15 min until 2 PM. SL = 75%, Target = 140% |

### Where to See Them

- **Dashboard:** Active suggestions appear in a card at the bottom with strike, entry, CMP, SL, target, confidence, and action buttons
- **Alerts page → Trade Signals tab:** Full view of active and past suggestions with Dismiss/Take Trade buttons

### How Suggestions Resolve

| Status | Meaning |
|--------|---------|
| PENDING | Awaiting sanity check |
| ACTIVE | Passed sanity check, waiting for user action |
| REJECTED_AUTO | Failed sanity check (signal weakened) |
| REJECTED_USER | User dismissed the suggestion |
| EXECUTED | User took the trade |
| SL_HIT | Stop loss was hit |
| EXPIRED | Stale (>24h old, no action taken) |

### Tips
- Only act on suggestions with confidence > 60%
- The system is intentionally conservative — you may go days without a suggestion
- Always verify the rationale before taking a trade
- These are directional buy trades, not hedges — use the hedge system for risk management

---

## Alerts — Setup Guide

Alerts notify you when important market conditions change. You get a toast notification on screen and the bell icon in the header shows the count of recent alerts.

The Alerts page has three tabs:
- **Active Alerts** — your armed alert rules
- **Alert History** — log of triggered alerts
- **Trade Signals** — auto-generated buy suggestions (see [Trade Suggestion Engine](#trade-suggestion-engine))

### How to Create an Alert

1. Go to the **Alerts** page
2. Click **+ Create Alert**
3. Choose a **Metric**, **Condition**, and **Threshold**
4. Set a **Cooldown** (how many minutes to wait before the same alert can fire again)
5. Click **Create**

### Recommended Alert Setups

#### Must-Have Alerts

| What to Monitor | Metric | Condition | Threshold | Why |
|-----------------|--------|-----------|-----------|-----|
| VIX spike | India VIX | Crosses Above | **20** | VIX above 20 means increased fear — your sold options may get expensive fast |
| VIX extreme | India VIX | Crosses Above | **25** | Danger zone — consider hedging all positions |
| CE risk high | CE Risk Score | Greater Than | **60** | Your call-side positions need attention |
| PE risk high | PE Risk Score | Greater Than | **60** | Your put-side positions need attention |
| CE hedge trigger | CE Risk Score | Greater Than | **65** | Time to hedge your call positions |
| PE hedge trigger | PE Risk Score | Greater Than | **65** | Time to hedge your put positions |

#### Position-Level Alerts (Create One Per Open Position)

| What to Monitor | Metric | Condition | Threshold | Why |
|-----------------|--------|-----------|-----------|-----|
| Delta getting risky | Position Delta | Greater Than | **0.25** | Your OTM position is becoming ATM — directional risk increasing |
| Delta danger zone | Position Delta | Greater Than | **0.35** | Position is nearly ITM — hedge or exit immediately |
| Premium doubled | Premium Multiplier | Greater Than | **2.0** | You've lost 1x your entry premium — review the position |
| Premium tripled | Premium Multiplier | Greater Than | **3.0** | Hard trigger level — hedge immediately |
| IV expanding | Position IV | Greater Than | **30** | Volatility is rising on your position — premium will increase against you |

#### Range & Spot Alerts

| What to Monitor | Metric | Condition | Threshold | Why |
|-----------------|--------|-----------|-----------|-----|
| NIFTY at key support | Nifty Spot | Crosses Below | *Your support level* | Spot broke a support — put positions at risk |
| NIFTY at key resistance | Nifty Spot | Crosses Above | *Your resistance level* | Spot broke a resistance — call positions at risk |
| Range breach | Range Breach | Greater Than | **50** | NIFTY has moved 50+ points outside the expected range — unusual move |
| Major range breach | Range Breach | Greater Than | **150** | Extreme move — check all positions immediately |

#### Alert Tips

- **Cooldown**: Set to 15-30 minutes for market-level alerts (VIX, Spot). For position alerts, 5-10 minutes is fine
- **Start simple**: Begin with the "Must-Have" alerts. Add position alerts as you take trades
- **Don't over-alert**: Too many alerts becomes noise. Focus on actionable thresholds
- **Review weekly**: Delete alerts for closed positions and adjust thresholds based on market conditions

---

## News Desk

The News Desk is a completely separate module that pulls market-relevant news from RSS feeds, scores each item for impact and directional bias, and surfaces the most important ones to you. **News data is fully siloed — it has no effect on the directional signal, risk scores, or any other indicator.**

### How News Is Fetched

The scheduler polls the following RSS feeds every 90 seconds during market hours (6 AM – 11 PM IST):

| Source | Weight |
|--------|--------|
| Zerodha Pulse (India market aggregator) | 1.0 |
| Bloomberg Markets | 1.1 |
| India News Network Business | 0.9 |
| Tickertape Blog | 0.8 |

If all 4 feeds return zero items, `gnews` is used as a silent fallback. Items older than 7 days are automatically cleaned up.

### Scoring

Each news item is scored by a pure rule-based scorer (no ML):

1. **Category matching** — keywords matched against 9 categories (RBI Policy, Nifty/Index, Derivatives, Macro/Global, Budget/Fiscal, Macro Data, Corporate, Geopolitical, Regulatory). Each category has a weight (e.g., RBI Policy = 2.5, Derivatives = 2.2).
2. **Directional scoring** — bullish and bearish word lists are scanned. Net winner determines direction (BULLISH / BEARISH / NEUTRAL).
3. **Raw score** = `(sum of category scores + |bullish − bearish| × 2.0) × source_weight`

| Tier | Score Threshold |
|------|----------------|
| CRITICAL | ≥ 80 |
| HIGH | ≥ 50 |
| MEDIUM | ≥ 25 |
| LOW | ≥ 10 |
| (filtered out) | < 5 |

Items scoring below 5 are never stored.

### News Desk Page (`/news-desk`)

- **Summary strip** — CRITICAL / HIGH / MEDIUM counts for the last 24h and a Market Tone badge (majority direction in the last 4h)
- **Filters** — filter by tier, direction, category, and time window (1h / 24h / 48h / 7d)
- **Feed** — cards showing tier badge, direction arrow, category chip, source, age, headline, and summary
- **Click any card** to expand it and see matched keywords by category, direction word counts, raw score, scorer version, and a link to the original article
- **Dismiss** button removes a card from your view (stored per-user, not visible in ticker either)
- **Pagination** — Prev/Next when results span multiple pages

### Data Isolation

News data is stored in two separate tables (`news_items`, `news_dismissals`) and served through its own router (`/api/news/*`). None of the following are read from or written to by the news system: directional signal computation, risk scores, hedge suggestions, trade suggestion engine, alert rules, or any existing API endpoints.

---

## Risk Scoring — How It Works

Every open position gets a risk score from 0 to 100. The score is calculated using 6 factors:

### The 6 Risk Factors

| Factor | Weight | What It Measures |
|--------|--------|-----------------|
| **Delta** | 25% | How much your option moves with NIFTY. Higher delta = more exposure |
| **Gamma/DTE** | 15% | How fast delta changes near expiry. In the last 7 days, gamma explodes |
| **IV Change** | 15% | How much implied volatility has risen since you entered. Rising IV = position getting expensive |
| **Premium Multiple** | 15% | Current price vs your entry price. If it's 3x your entry, that's a big loss |
| **Proximity** | 20% | How close NIFTY is to your strike. Closer = more dangerous |
| **Theta Ratio** | 10% | Is the time decay compensating you enough for the risk you're taking? |

### Score Meaning

| Score | Label | What to Do |
|-------|-------|-----------|
| 0 - 34 | **OK** (green) | Position is safe. No action needed |
| 35 - 59 | **WARNING** (amber) | Keep an eye on it. Consider setting alerts |
| 60 - 79 | **DANGER** (red) | Actively monitor. Prepare a hedge plan |
| 80 - 100 | **CRITICAL** (dark red) | Hedge immediately or exit the position |

### Hard Triggers (Instant Hedge Alert)

These conditions bypass the score and immediately flag "HEDGE REQUIRED":

| Trigger | What It Means |
|---------|--------------|
| Delta >= 0.35 | Your option is nearly ITM — very high directional risk |
| Premium >= 3x entry | You've lost 2x your collected premium — bleeding money |
| Spot within 0.5% of strike | NIFTY is right at your strike — you're about to go ITM |
| VIX >= 28 AND VIX change >= 25% | Extreme fear spike — all short option positions are at risk |
| DTE <= 5 AND Delta >= 0.15 | Near expiry with meaningful delta — gamma risk is very high |

---

## Hedge Suggestions

When a hedge is needed, the app automatically suggests the best option to buy as protection.

### How the Hedge is Chosen

The app evaluates all available strikes and scores them on 6 factors:

1. **Cost Efficiency** — How much protection you get per rupee spent
2. **Delta Coverage** — How well the hedge offsets your sold position's delta
3. **Gamma Advantage** — How much the hedge benefits from fast moves (important near expiry)
4. **Liquidity** — Higher OI and volume = easier to buy at a fair price
5. **IV Skew** — Adapts to whether calls or puts are currently expensive
6. **S/R Proximity** — Prefers strikes near support/resistance levels (natural turning points)

### What the Weights Look Like

The app automatically adjusts which factors matter most based on how many days are left:

| Days to Expiry | Priority |
|---------------|----------|
| 4+ days | Cost efficiency is king (30%) — you have time, buy cheap protection |
| 2-3 days | Delta coverage matters more (25%) — protect the direction |
| 0-1 days | Gamma is everything (25%) — you need instant protection |

### Reading the Suggestion

- **Strike & LTP**: The option strike and price to buy
- **Qty**: How many lots (auto-calculated, max 3 lots)
- **Cost**: Total premium to pay (LTP x lots x 65)
- **Worst P&L**: Your total P&L if NIFTY moves 200 points against you (including the hedge)
- **Best P&L**: Your P&L if NIFTY stays put and you keep your sold premium
- **Confidence**: 0-100 score. Above 60 = strong suggestion. Below 40 = weak (consider alternatives)
- **Rationale**: Short explanation (e.g., "OTM 150pts @45.0 | near S/R | gamma-weighted")

---

## Directional Signal

The Directional Signal is an 8-factor system that tells you if the overall option market is leaning bullish, bearish, or neutral.

### The 8 Factors

| # | Factor | Bullish When | Bearish When |
|---|--------|-------------|-------------|
| 1 | **Risk Reversal** | Calls are expensive (positive RR) | Puts are expensive (negative RR) |
| 2 | **Put-Call Ratio** | High PCR (>1.2) — lots of put writing = support | Low PCR (<0.8) — lots of call writing = resistance |
| 3 | **OI Buildup** | Put OI building below spot (support forming) | Call OI building above spot (ceiling forming) |
| 4 | **IV Skew** | Call-wing IV steeper than put-wing | Put-wing IV steeper than call-wing |
| 5 | **Volume** | More CE volume near ATM (buyers want calls) | More PE volume near ATM (buyers want puts) |
| 6 | **VIX** | Low and falling VIX (calm market) | High and rising VIX (fearful market) |
| 7 | **Max Pain** | Spot below max pain (gravitational pull up) | Spot above max pain (gravitational pull down) |
| 8 | **GEX** | Positive gamma near spot (market stabilizes) | Negative gamma near spot (moves get amplified) |

### How to Read It

- **BULLISH (X/8 agree)**: 5 or more factors are pointing up. Market is likely to rise
- **BEARISH (X/8 agree)**: 5 or more factors are pointing down. Market is likely to fall
- **NEUTRAL (X/8 agree)**: Less than 5 factors agree on either side. No clear direction
- The **Score** shows the total strength: positive = bullish bias, negative = bearish bias

### What to Do With It

- If you're an option seller, the signal helps you decide which side to sell:
  - BULLISH signal → safer to sell puts (PE SELL)
  - BEARISH signal → safer to sell calls (CE SELL)
  - NEUTRAL → sell both sides (strangle) or wait
- The signal is one input — always consider your own analysis too

---

## Support & Resistance Levels

### OI + Fibonacci (Highest Reliability)

The app combines two methods to find the strongest levels:

1. **Open Interest**: Strikes where the most options are written. Large OI acts like a wall — NIFTY tends to pause there
2. **Fibonacci Retracement**: Mathematical levels based on NIFTY's recent high and low

When both methods agree on the same level (within 75 points), it gets marked as a **confluence** level — shown with a highlighted border on the dashboard. These are the most reliable levels.

### Pivot + OI

Standard pivot points (calculated from yesterday's high, low, close) ranked by how much Open Interest supports them. Useful for intraday reference.

---

## Settings & Preferences

### Profile Card

Click your name or avatar in the top-right header to open the floating profile card. It shows:
- Your name and email
- A **Dark Mode** toggle switch (animates on/off, persists across sessions)
- A **Sign Out** button

The card closes by clicking outside it or pressing Escape.

### VIX Offset %

Found on the Dashboard chart card. Controls how wide the "VIX Range" amber band is.

- **0%** = same as the calculated range (1 standard deviation)
- **50%** (default) = 1.5x the standard move — gives you more buffer
- **100%** = 2x the standard move — very conservative
- Default resets to 50% on each page load for consistency

### Expiry Selector

The dropdown in the header shows **all available NIFTY expiries** fetched live from NSE — this works even when the market is closed. Selecting a different expiry recalculates:
- Calculated range and VIX offset range
- Days to expiry
- 25-delta Risk Reversal (CE/PE IV, RR value, verdict)
- Tendency
- Directional Signal (8 factors)
- Max Pain
- OI + Fibonacci S/R levels
- Pivot + OI S/R levels

Your last expiry selection is saved in localStorage and restored on the next visit — past expiries are automatically filtered out.

---

## Running the App

### Requirements

- Python 3.11+
- pip packages listed in `backend/requirements.txt`

### Install & Start

```bash
# Install dependencies
pip install -r backend/requirements.txt

# Start the server
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in your browser.

### Dev Tip — Seeing CSS/JS Changes

The browser caches static files aggressively. After making frontend changes, use **Ctrl + Shift + R** (hard refresh) to bypass the cache, or open DevTools → Network tab → check **"Disable cache"** (only active while DevTools is open).

### Market Hours

The app fetches live data from NSE only during market hours:
- **Weekdays 9:00 AM to 3:45 PM IST** (excluding NSE holidays)
- Outside these hours, you'll see a "Market Closed" banner with the next opening time
- Your last known data is still visible — it just won't update until market opens

### Data Refresh

- **Every 15 seconds**: Spot price, VIX, option LTP, P&L, risk scores
- **Every 3 minutes**: S/R levels, Fibonacci, Pivot points
- **Every 90 seconds** (6 AM – 11 PM IST): News RSS fetch, score, and store
- All updates happen automatically via WebSocket — no need to refresh the page

---

## Quick Reference

### Risk Score Cheat Sheet

| Score | Color | Action |
|-------|-------|--------|
| 0-34 | Green | Relax. Position is comfortable |
| 35-59 | Amber | Watch it. Set alerts if you haven't |
| 60-64 | Red | Prepare a hedge. Check the suggestion |
| 65+ | Dark Red | Hedge now. The app will show you what to buy |

### Alert Priority Guide

Set these alerts first (most important to least):

1. VIX > 20 (market-wide warning)
2. CE/PE Risk > 60 (position warning)
3. Position Delta > 0.25 (per position)
4. Premium Multiplier > 2.0 (per position)
5. Range Breach > 50 (unusual move)

### Key Numbers to Remember

| Number | Meaning |
|--------|---------|
| **65** | NIFTY lot size (1 lot = 65 units, as of April 2026) |
| **0.25 delta** | The "comfort zone" boundary — above this your OTM option is getting risky |
| **0.35 delta** | Hard trigger — hedge immediately |
| **3x premium** | Hard trigger — your option has tripled against you |
| **VIX 20** | Elevated volatility — be cautious |
| **VIX 28** | Extreme volatility — all positions at risk |
| **65 risk score** | Hedge trigger threshold |

---

*Built for NIFTY option sellers who want to sleep well at night.*
