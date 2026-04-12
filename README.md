# NiftyEdge — NIFTY Options Trading Dashboard

A real-time web app for NIFTY options traders to monitor positions, track risk, get hedge suggestions, and spot directional opportunities — all in one place.

---

## Table of Contents

1. [Getting Started](#getting-started)
2. [Dashboard](#dashboard)
3. [Option Chain](#option-chain)
4. [Positions](#positions)
5. [Trade Journal](#trade-journal)
6. [Analytics](#analytics)
7. [Alerts — Setup Guide](#alerts--setup-guide)
8. [Risk Scoring — How It Works](#risk-scoring--how-it-works)
9. [Hedge Suggestions](#hedge-suggestions)
10. [Directional Signal](#directional-signal)
11. [Support & Resistance Levels](#support--resistance-levels)
12. [Settings & Preferences](#settings--preferences)
13. [Running the App](#running-the-app)

---

## Getting Started

### First-Time Setup

1. Open the app in your browser (default: `http://localhost:8000`)
2. Click **Sign Up** and enter your name, email, and a password (minimum 6 characters)
3. You'll be logged in automatically and taken to the Dashboard

### Logging In

- Enter your email and password on the login page
- You stay logged in for 7 days — no need to log in every time
- Click your name in the top-right corner to access **Dark Mode** toggle or **Logout**

---

## Dashboard

The Dashboard is your command center. Everything updates automatically in real-time when the market is open.

### Market Overview Strip

At the top, you'll see these key numbers:

| Card | What It Shows |
|------|--------------|
| **Nifty Spot** | Current NIFTY index price with today's change |
| **India VIX** | Fear gauge — higher VIX = more volatility expected |
| **Days to Expiry** | Trading days left until the nearest expiry |
| **Calc Range** | Expected price range for NIFTY based on VIX (1 standard deviation) |
| **Tendency** | Simple market mood — Positive, Negative, or Neutral (based on option skew + VIX) |
| **Max Pain** | The strike where option writers make the most money — NIFTY tends to gravitate here near expiry (updates during market hours only) |
| **Directional** | Advanced 8-factor signal — BULLISH, BEARISH, or NEUTRAL with how many factors agree (updates during market hours only) |

### Range & Spot Monitor Chart

A visual chart showing:
- **Blue band** = Calculated Range (1 standard deviation move)
- **Amber band** = VIX Range (wider, based on your VIX Offset setting)
- **Red diamond** = Current NIFTY spot price

**VIX Offset %** (top-right of chart): Adjusts how wide the amber range band is. Default is 50%. If you want a wider safety margin, increase it. The chart updates instantly.

### Support & Resistance

Shows the strongest price levels where NIFTY is likely to pause or reverse:

- **OI + Fibonacci**: Levels where high Open Interest coincides with Fibonacci retracement — these are the most reliable
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
4. Click any strike row to look up its details

### Strike Lookup

Two cards at the top let you look up any CE or PE strike:
- See LTP, Delta, IV, Gamma, Theta, OI, Volume
- See **Premium Ranking** — tells you if the premium is:
  - **Very Low** — cheap premium, far OTM
  - **Sweet** — good value zone for selling
  - **Slightly Higher** — paying a bit more
  - **High** — expensive premium
- Click **Add as Trade** to quickly add it to your positions

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

---

## Alerts — Setup Guide

Alerts notify you when important market conditions change. You get a toast notification on screen and the bell icon in the header shows the count of recent alerts.

### How to Create an Alert

1. Go to the **Alerts** page
2. Click **+ Create Alert**
3. Choose a **Metric**, **Condition**, and **Threshold**
4. Set a **Cooldown** (how many minutes to wait before the same alert can fire again)
5. Click **Create**

### Recommended Alert Setups

Here are the alerts every NIFTY option seller should set up:

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
- **Cost**: Total premium to pay (LTP x lots x 75)
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

### VIX Offset %

Found on the Dashboard chart card. Controls how wide the "VIX Range" amber band is.

- **0%** = same as the calculated range (1 standard deviation)
- **50%** (default) = 1.5x the standard move — gives you more buffer
- **100%** = 2x the standard move — very conservative
- Your setting is saved and remembered across sessions

### Dark Mode

Click your name in the top-right corner, then click **Dark Mode** to switch. Your preference is remembered.

### Expiry Selector

The dropdown in the header lets you switch between available expiries. The app defaults to the nearest weekly expiry.

---

## Running the App

### Requirements

- Python 3.11+
- pip packages: `fastapi uvicorn sqlalchemy pnsea numpy scipy pandas yfinance bcrypt pyjwt`

### Install & Start

```bash
# Install dependencies
pip install fastapi uvicorn sqlalchemy pnsea numpy scipy pandas yfinance bcrypt pyjwt

# Start the server
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in your browser.

### Market Hours

The app fetches live data from NSE only during market hours:
- **Weekdays 9:00 AM to 3:45 PM IST** (excluding NSE holidays)
- Outside these hours, you'll see a "Market Closed" banner with the next opening time
- Your last known data is still visible — it just won't update until market opens

### Data Refresh

- **Every 15 seconds**: Spot price, VIX, option LTP, P&L, risk scores
- **Every 3 minutes**: S/R levels, Fibonacci, Pivot points
- All updates happen automatically — no need to refresh the page

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
| **75** | NIFTY lot size (1 lot = 75 units) |
| **0.25 delta** | The "comfort zone" boundary — above this your OTM option is getting risky |
| **0.35 delta** | Hard trigger — hedge immediately |
| **3x premium** | Hard trigger — your option has tripled against you |
| **VIX 20** | Elevated volatility — be cautious |
| **VIX 28** | Extreme volatility — all positions at risk |
| **65 risk score** | Hedge trigger threshold |

---

*Built for NIFTY option sellers who want to sleep well at night.*
