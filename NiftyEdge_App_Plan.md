# NiftyEdge — Product Plan & Architecture

> A personalized NIFTY options trading dashboard replacing the current Google Sheets system.  
> Target: 5–10 users, each managing their own book on NIFTY options.

---

## 1. Core product principles

- **Each user owns their data.** Same market feed, completely independent positions, P&L, alerts, and analytics.
- **The dashboard is the home screen.** 90% of time is spent here. Everything else is one click away.
- **Real-time feel, not real-time cost.** 15–30 second periodic refresh via PNSEA is sufficient. WebSocket push keeps UI snappy without polling.
- **Laptop-first responsive.** Optimized for 1366×768 to 1920×1080. Scales up to large monitors and down to tablets.

---

## 2. User flow

```
Signup/Login → Dashboard (default landing)
                 ├── View market overview (Nifty, VIX, ranges, S/R)
                 ├── View risk meter for current positions
                 ├── Quick-glance open positions summary
                 │
                 ├── Option chain tab
                 │     ├── Full chain table (CE OI, CE IV, LTP, CE Delta, Strike, PE Delta, LTP, PE IV, PE OI)
                 │     ├── Strike lookup (CE and PE side, shows LTP, Delta, IV, Premium Ranking)
                 │     ├── Click row to auto-populate lookup
                 │     └── Quick "Add trade" from lookup to Positions
                 │
                 ├── Positions tab
                 │     ├── Add new trade (CE/PE sell or hedge buy)
                 │     ├── View all open positions (grouped by expiry)
                 │     ├── Add hedge to existing position
                 │     ├── Close position (current LTP or manual)
                 │     └── View unrealized P&L per position
                 │
                 ├── Analytics tab
                 │     ├── Performance overview (cumulative P&L, win rate)
                 │     ├── Strike selection patterns
                 │     ├── IV/Delta historical tracking
                 │     ├── Multi-expiry comparison
                 │     └── Range backtest
                 │
                 ├── Trade journal tab
                 │     ├── All closed trades with entry/exit details
                 │     ├── P&L by expiry
                 │     └── Realized P&L history
                 │
                 └── Alerts tab
                       ├── Create alert rules
                       ├── Active alerts list
                       ├── Alert history/log
                       └── Notification preferences
```

---

## 3. Screen-by-screen breakdown

### 3.1 Login / Signup

Simple auth page. No RBAC needed — all users have identical permissions.

**Fields:** Email, password, name (for signup).  
**Features:** "Remember me" toggle, password reset flow.  
**Post-login:** Redirect to Dashboard.

---

### 3.2 Dashboard (command center)

This is the sheet replacement — all 7 sheet sections consolidated into one view.

**Layout:** Header bar + sidebar + two-column main area.

#### Header bar (persistent across all screens)
- App logo/name with colored icon badge (left)
- **Global expiry selector** (dropdown) — fetched **live from NSE** via `/api/market/expiries`, works even when market is closed. Selecting a non-default expiry calls `/api/market/compute-for-expiry` which recalculates: calc range, VIX offset range, DTE, tendency, 25-delta RR, directional signal, max pain, and OI+Fib / Pivot+OI S/R levels — all for the selected expiry chain.
- Market status badge (Market Open / Market Closed)
- Notification bell with unread count
- User avatar with dropdown (dark mode toggle, logout)

#### Sidebar (icon-based, collapsible)
- Dashboard, Option Chain, Positions, Analytics, Journal, Alerts
- Active indicator on current page

#### Left column (60% width) — Market & range
- **Market overview strip:** 5 metric cards in a row
  - Nifty spot price (live)
  - India VIX (with % change)
  - Days to expiry
  - Expected move (±points)
  - Market tendency (Negative/Neutral/Positive)

- **Range & spot monitor (interactive chart)**
  - Vertical axis = Nifty levels
  - Visual zones: VIX range (yellow), calculated range (gray), traded range (blue)
  - CE and PE strike markers with color coding
  - CMP diamond marker (animates on update)
  - Support/resistance level markers (S1-S3, R1-R3)
  - Hover tooltips on each zone showing exact values
  - This replaces the static Image 7 chart with a smooth, interactive canvas

- **Support/resistance levels**
  - Two sub-sections: OI+Pivot based and OI/Fib-Ret based
  - Support levels in green pills, resistance in red pills
  - Clickable to set as alert trigger

- **25-delta risk reversal**
  - Upper and lower values with IV
  - Verdict badge (Neutral/Bullish/Bearish with conviction level)
  - Tendency indicator

#### Right column (40% width) — Risk & positions

- **Risk meter (dual gauge)**
  - CE Risk: score/100 with progress bar and status (Safe/Warning/Danger)
  - PE Risk: score/100 with progress bar and status
  - Overall verdict badge (Balanced/Skewed/Critical)
  - Color transitions: green (0-30), amber (30-60), red (60-100)

- **Open positions summary (compact)**
  - Shows each open leg as a row: type badge, strike, current IV with % change, current delta
  - "Add trade" link
  - "View all" link to Positions tab
  - Shows max 4 positions; overflows to "View all"

- **Risk detail table**
  - CE side vs PE side comparison
  - Rows: Delta, IV, Premium, Proximity
  - Each cell: OK (green check) or Warning (amber) or Danger (red)
  - Tooltip on each cell showing the exact values (e.g., "Δ = 0.073, OK")

---

### 3.3 Option chain (live chain viewer + strike lookup)

A dedicated page for browsing the live NIFTY option chain and quickly looking up any strike's details. This replaces the separate Option Chain sheet.

**Layout:** Strike lookup panel (top) + full chain table (below).

#### Strike lookup panel (persistent at top)
Two side-by-side cards — one for CE, one for PE:

- **CE lookup card:**
  - Strike input field — user can manually type any strike and press Enter or click Lookup
  - Results: LTP, Delta, IV, Premium Ranking badge
  - Premium Ranking uses color-coded badges:
    - "Very Low" (red) — premium too thin, not worth selling
    - "Low" (amber) — below average, proceed with caution
    - "Sweet" (green) — ideal premium-to-risk ratio
    - "High" (blue) — rich premium, but closer to money
    - "Very High" (purple) — deep ITM or extreme premium

- **PE lookup card:** Same structure as CE but for put side

- **Click-to-lookup logic (OTM-aware):**
  When a user clicks a strike row in the chain table, the system determines which side to fill based on the strike's position relative to ATM (current Nifty spot):
  - **Clicked strike > ATM** → fills the **CE lookup** panel only (it's OTM for calls, which is the relevant side for CE sellers)
  - **Clicked strike < ATM** → fills the **PE lookup** panel only (it's OTM for puts, relevant for PE sellers)
  - **Clicked strike = ATM** → fills whichever panel is currently empty; if both are filled, fills CE by default
  - Each click fills only ONE side. User must click two different strikes (one above ATM, one below) to populate both CE and PE lookups.
  - Example: Nifty at 22700. User clicks 21200 → PE lookup fills (21200 is OTM for puts). User then clicks 24000 → CE lookup fills (24000 is OTM for calls). Now both panels show the user's chosen strangle.

- **Manual entry always available:** Users can also type any strike directly into the CE or PE input field and hit Enter/Lookup. This overrides any click-populated value. Manual entry does not enforce OTM logic — a user can manually look up an ITM strike if they want to.

- **Quick action:** "Add as trade" button in each lookup result — pre-fills the Add Trade modal in Positions with that strike's data (strike, current premium, IV, delta all auto-captured)

#### Option chain table
A mirrored table with Strike as the center column — CE data on the left, PE data on the right.

**Columns (left to right):**
CE OI | CE IV | LTP (CE) | CE Delta | **Strike** | PE Delta | LTP (PE) | PE IV | PE OI

**Table features:**
- **ATM highlight:** The strike closest to current Nifty spot is highlighted in amber/gold. Auto-updates as spot moves.
- **Open position markers:** If the user has an open trade at a strike, show a small colored dot next to that strike (red dot for CE positions, blue for PE).
- **Scrollable with sticky header:** Table scrolls vertically; header row stays pinned.
- **Strike increment:** 50-point intervals (20700, 20750, 20800...), matching NSE option chain.
- **Empty IV/Delta:** Strikes far OTM show "-" for IV and Delta when data is 0 or unavailable.
- **OI formatting:** Large numbers formatted with commas for readability.
- **Row hover:** Hover highlights the full row across both CE and PE sides.
- **Row click:** Clicking a row fills the appropriate lookup panel (CE if strike > ATM, PE if strike < ATM) and smooth-scrolls to the top so the user can see the populated result.
- **Data refresh:** Entire table refreshes every 15-30 seconds in sync with the main data feed.

**Enhancements over the sheet:**
- In the sheet, you manually type a strike into the lookup section. In the app, you can still type manually OR click any row — clicks intelligently fill only the OTM side (PE for strikes below ATM, CE for strikes above ATM), so two clicks build your strangle lookup instantly.
- Premium Ranking is calculated live (not just for the looked-up strike but can be shown as a subtle background tint on the LTP columns — green tint for "sweet" range, red for "very low").
- A small toggle to filter the table to show only strikes within the calculated range or all strikes.
- Option to sort by OI (highest OI strikes first) to quickly spot max pain / key support-resistance levels.

---

### 3.4 Positions (trade management)

The biggest upgrade from sheets — full CRUD for positions with P&L tracking.

#### Open positions list
- **Filter bar:** Expiry dropdown, type filter (CE/PE/All), sort by (strike, P&L, risk)
- **"+ Add trade" button** (opens modal)

- **Position cards** (one card per leg):
  - Header: Type badge (CE SELL / PE SELL / CE BUY / PE BUY), strike, expiry, quantity
  - Action buttons: "Add hedge", "Close"
  - Metrics grid (6 columns): Entry IV, Current IV (with % change), Entry Delta, Current Delta (with % change), Entry Premium, Current LTP
  - Unrealized P&L bar at bottom (green for profit, red for loss)
  - If hedge exists: nested hedge card appears below with same structure but labeled as "Hedge"

- **Grouping:** Positions grouped by expiry. Each expiry is a collapsible section.

#### Add trade modal
- Fields: Type (CE SELL / PE SELL / CE BUY hedge / PE BUY hedge), Expiry (dropdown), Strike (number input), Entry Premium (number input), Quantity (number input)
- If adding a hedge: dropdown to select which open sell position this hedges
- Entry IV and Entry Delta are **auto-captured** from live data at submission time
- Traded date is auto-set to current timestamp

#### Close position modal
- Shows: Position summary (type, strike, expiry, quantity)
- **Close price selector:**
  - Radio: "Current LTP" (shows live price, updates every refresh) OR "Manual"
  - If manual: number input for exit price
- **Close quantity:** Number input with "All" button (defaults to full quantity for complete close, editable for partial close)
- **P&L preview:** Shows calculated realized P&L before confirmation
  - Formula shown: (entry premium − exit price) × quantity for sells
  - Formula shown: (exit price − entry premium) × quantity for buys
- Confirm/Cancel buttons
- On confirm: Position moves to trade journal, realized P&L is recorded

#### Preventive hedge trades section
- Separate section at bottom showing suggested/active hedges
- For each: Side (CE BUY / PE BUY), Strike, Qty, Hedge Cost, Worst Case P&L, Best Case P&L, Confidence Score
- If no hedges needed: shows "Not Required" status

---

### 3.5 Analytics (intelligence layer)

Six sub-tabs, each providing different analytical views.

#### Tab 1: Performance overview
- **Summary metrics** (4 cards): Total realized P&L, Win rate (wins/total), Avg profit per expiry, Max drawdown
- **Cumulative P&L curve:** Line chart with time filter (1M, 3M, 6M, All). Hover shows exact values.
- **P&L by expiry:** Bar chart, green bars for profit, red for loss. Hover shows details.
- **Monthly performance heatmap:** Calendar-style grid, color intensity = P&L magnitude.

#### Tab 2: Strike selection patterns
- **CE strike distance histogram:** Distribution of how far OTM the user sells CEs. Insight text: "You favor 900pt OTM CEs — winning 82% there."
- **PE strike distance histogram:** Same for PEs.
- **Strike vs outcome scatter:** Strike distance on X, P&L on Y. Each dot = one trade. Color = win/loss. Shows the user's sweet spot.
- **Win rate by VIX regime:** Horizontal bars showing win% for VIX <15, 15-25, >25.
- **Insights panel:** Auto-generated text observations. Examples:
  - "Your win rate drops 25% when VIX > 25 — consider reducing size."
  - "You've never lost when selling CE > 1000pts OTM."

#### Tab 3: IV / Delta historical tracking
- **IV chart by strike over time:** Select a strike or "all active strikes." Line chart showing IV trajectory from entry to expiry.
- **Delta chart by strike over time:** Same structure for delta decay.
- **IV at entry vs outcome:** Scatter showing entry IV on X, P&L on Y. Reveals if higher IV entries perform better.
- **Historical IV percentile:** Where current IV sits relative to last 30/60/90 days.
- Filters: Expiry range, strike type (CE/PE), date range.

#### Tab 4: Multi-expiry comparison
- **Side-by-side expiry cards:** For 2–4 expiries, show calculated range, expected move, VIX at time, positions taken, P&L outcome.
- **Overlay chart:** Multiple expiry ranges overlaid on a single vertical axis to compare width, skew, and outcome.
- **Expiry timeline:** Horizontal scrollable timeline showing each expiry with a mini summary card.

#### Tab 5: Range backtest
- **Input panel:** Select historical date range, VIX offset %, range calculation method.
- **Backtest results:** How often spot stayed within calculated range. Hit rate displayed.
- **Range accuracy chart:** For each historical expiry, show the predicted range vs actual spot movement. Visual: predicted range as a band, actual as a line.
- **Suggested calibration:** Based on backtest, suggest optimal VIX offset or range width.

#### Tab 6: Historical Insights (NEW)
- **EOD signal snapshot capture:** Every trading day at ~15:30, the system records Nifty close, VIX, RR value/verdict, tendency, directional direction/score/factors, and max pain.
- **Next-day retroactive backfill:** The following day's close is written back so signal accuracy can be measured.
- **Summary cards:** Total signals captured, bullish accuracy %, bearish accuracy %, tendency agreement %.
- **Dual-axis chart:** Signal score (line, blue) vs next-day Nifty change % (bars, green/red). Dots colored by correctness.
- **Signal history table:** Scrollable table of every daily snapshot with date, close, VIX, RR, tendency, direction, score, next-day change, and correct/incorrect badge.
- **API endpoints:** `GET /api/analytics/historical-signals?days=90`, `GET /api/analytics/signal-accuracy?days=90`.

---

### 3.6 Trade journal

Simple, clean historical record of all closed trades.

#### Closed trades table
- Columns: Date, Type, Strike, Expiry, Entry Premium, Exit Price, Quantity, Realized P&L, Duration (days held)
- Sortable by any column
- Filterable by: Date range, Type (CE/PE), Profit/Loss, Expiry
- Color coding: green row tint for profit, red for loss

#### P&L summary panel (above table)
- Total realized P&L (all time)
- This month's P&L
- Average P&L per trade
- Best trade / Worst trade

#### Expiry-wise log
- Grouped view: each expiry is a collapsible section showing all trades for that expiry
- Expiry summary: total premium collected, total premium paid (hedges), net P&L, number of legs

---

### 3.7 Alerts & notifications

#### Create alert
- **Alert types:**
  - Nifty spot crosses a level (above/below threshold)
  - VIX crosses a level
  - Delta of a specific position exceeds threshold
  - IV of a specific position changes by X%
  - Risk score exceeds threshold (CE or PE side)
  - Premium of position doubles/triples from entry (for stop-loss)
  - Nifty enters/exits calculated range
  - Custom: any metric comparison

- **Alert form:** Metric dropdown → Condition (>, <, crosses above, crosses below) → Value → Notification method (in-app, browser push, optional email/webhook)

#### Active alerts list
- Each alert as a card: Metric, condition, status (armed/triggered/paused)
- Toggle to enable/disable
- Edit and delete actions

#### Alert history
- Log of all triggered alerts with timestamp and the value at trigger time
- Useful for reviewing what happened and when

#### Trade Signals tab (NEW)
- **Active trade suggestions:** Shows pending auto-generated buy suggestions with strike, entry, CMP, SL, target, live P/L %, confidence, rationale, and expiry. User can **Dismiss** or **Take Trade**.
- **Past suggestions:** Historical list of resolved suggestions (rejected, executed, expired, SL hit) with the same detail cards.
- **Real-time updates:** Refreshes via WebSocket `trade_suggestions` broadcast when the signals tab is open.
- **API endpoints:** `GET /api/suggestions/` (active), `GET /api/suggestions/history` (past), `POST /api/suggestions/{id}/reject`, `POST /api/suggestions/{id}/execute`.

#### Notification center
- Bell icon in header with unread count badge
- Dropdown panel showing recent notifications
- Each notification: alert name, triggered value, timestamp, link to relevant position/dashboard section

---

## 4. Data model (key entities)

### User
- id, email, password_hash, name, created_at, settings (JSON)

### Trade (open or closed)
- id, user_id, type (CE_SELL, PE_SELL, CE_BUY, PE_BUY), strike, expiry_date
- entry_premium, entry_iv, entry_delta, entry_date, quantity
- exit_price, exit_date (null if open)
- status (OPEN, CLOSED)
- parent_trade_id (null for sells, references sell trade_id for hedges)
- realized_pnl (calculated on close)

### Market snapshot (shared across users)
- timestamp, nifty_spot, india_vix, vix_change_pct
- calculated_range_top, calculated_range_bottom
- vix_range_top, vix_range_bottom
- expected_move, days_to_expiry, tendency
- expiry_date, expiries_json (full list of available expiries)
- max_pain, directional_direction, directional_score, directional_factors_agree
- rr_ce_strike, rr_ce_iv, rr_pe_strike, rr_pe_iv, rr_value, rr_verdict

### Strike data (per strike per timestamp)
- timestamp, strike, expiry_date, option_type (CE/PE)
- ltp, iv, delta, oi, volume

### Alert rule
- id, user_id, metric, condition, threshold, notification_method
- status (ARMED, TRIGGERED, PAUSED), last_triggered_at

### Alert log
- id, alert_rule_id, triggered_at, triggered_value, message

### Support/resistance levels (per refresh)
- timestamp, expiry_date, source (OI_PIVOT, OI_FIB)
- support_levels (array), resistance_levels (array)

### Daily signal snapshot (NEW — EOD capture for historical insights)
- id, date (unique), nifty_close, india_vix
- rr_value, rr_verdict, tendency, directional_direction, directional_score, directional_factors_agree, max_pain
- next_day_close, next_day_change, next_day_change_pct, signal_correct (backfilled retroactively)

### Trade suggestion (NEW — auto-generated buy signals)
- id, suggestion_type (OVERNIGHT/INTRADAY), direction (BULLISH/BEARISH), option_type (CE/PE)
- strike, entry_price, stop_loss, target, expiry_date
- directional_score, directional_factors, tendency, rr_value, confidence (0-100), rationale
- status (PENDING/ACTIVE/REJECTED_AUTO/REJECTED_USER/EXECUTED/EXPIRED/SL_HIT)
- created_at, sanity_checked_at, resolved_at, current_price, nifty_spot_at_creation

---

## 5. Data flow

```
Every 15-30 seconds:
  ┌──────────────┐
  │  PNSEA        │──→ Scrapes NSE Option Chain
  │  (Python)     │──→ Gets Nifty Spot, VIX
  └──────┬───────┘
         │
         ▼
  ┌──────────────┐
  │  Backend      │──→ Stores market snapshot in DB
  │  Engine       │──→ Recalculates: ranges, S/R levels, risk reversal
  │               │──→ For EACH user with open positions:
  │               │      - Fetches current LTP/IV/Delta for their strikes
  │               │      - Recalculates risk meter scores
  │               │      - Checks alert rules against new data
  │               │      - Computes unrealized P&L
  └──────┬───────┘
         │
         ▼ (WebSocket push)
  ┌──────────────┐
  │  Frontend     │──→ Updates dashboard in real-time
  │  (per user)   │──→ Updates position cards with new LTP/IV/Delta
  │               │──→ Fires browser notifications if alerts triggered
  └──────────────┘
```

---

## 6. UI design guidelines

### Style direction
- **Not GenZ, not old-school.** Think: Bloomberg Terminal meets a modern SaaS dashboard.
- Clean, professional, data-dense but not cluttered.
- Muted color palette with strategic use of color for status/alerts.
- Dark mode support (traders often prefer it).

### Color system
- **Background:** Slate-tinted off-white (#F0F2F5) for main, white (#FFFFFF) for cards — avoids the "all white" problem
- **Sidebar:** Dark (#111827) in both themes
- **Text:** Near-black (#111827) primary, muted slate (#475569) secondary, light gray (#94A3B8) muted
- **Green:** Profit, safe, OK states (#16A34A)
- **Red:** Loss, danger, critical states (#DC2626)
- **Amber:** Warning, caution (#D97706)
- **Blue:** Info, neutral highlights, primary accent (#2563EB)
- **Purple:** Analytics/insights accent (#7C3AED)
- **Borders:** Soft slate (#CBD5E1 / #E2E8F0) for depth without harshness
- **Dark mode:** Deep navy backgrounds (#0B0F19, #111827) with soft borders (#1E293B)

### Typography
- System font stack (Inter or similar sans-serif)
- Metric numbers: tabular figures (monospace numerals for alignment)
- Font sizes: 11px labels, 13px body, 15px section headers, 18-24px key metrics

### Responsive breakpoints
- **Primary (1366-1920px):** Full two-column dashboard layout
- **Large monitors (>1920px):** Wider columns, more horizontal space
- **Tablets (768-1366px):** Stack columns vertically, collapsible sidebar

### Interaction patterns
- Cards for position management (not table rows)
- Modals for add/close actions
- Tooltips for detailed values on hover
- Smooth transitions on data updates (number tween, not hard swap)
- Toast notifications for alerts

---

## 7. Implementation phases (suggested)

### Phase 1: Foundation (MVP)
- Auth (login/signup)
- Dashboard with live market data (spot, VIX, ranges, S/R)
- Range & spot monitor chart
- Option chain page with full table and strike lookup
- Basic position entry (add trade)
- Position list with live IV/Delta/LTP updates
- Risk meter

### Phase 2: Position management
- Close position flow (current LTP + manual)
- Partial close support
- Realized P&L calculation and storage
- Hedge trade linking (buy linked to sell)
- Trade journal (closed trades list)

### Phase 3: Analytics
- Performance overview (P&L curve, win rate, expiry bars)
- Strike pattern analysis
- IV/Delta historical tracking
- Basic insights generation

### Phase 4: Advanced features
- Multi-expiry comparison
- Range backtest engine
- Alert system (create, manage, trigger)
- Browser push notifications
- Dark mode
- Export/download reports

---

## 8. Key differences from the sheet

| Capability | Google Sheet | NiftyEdge App |
|---|---|---|
| Multiple users | Messy, shared cells | Clean per-user isolation |
| Close a position | Manual cell editing | One-click with P&L preview |
| Partial closes | Not supported | Built-in quantity selector |
| Hedge linking | Separate section | Nested under parent sell |
| Realized P&L | Manual calculation | Auto-calculated, stored |
| Historical trades | Overwritten each week | Permanent journal |
| Performance trends | Not possible | Full analytics suite |
| Strike patterns | Not tracked | Auto-analyzed with insights |
| IV/Delta history | Lost after refresh | Stored and charted |
| Multi-expiry | One tab per expiry | Side-by-side comparison |
| Range backtest | Not possible | Historical accuracy testing |
| Alerts | Not possible | Rule-based with notifications |
| Option chain browsing | Separate sheet, manual lookup | Integrated page, click-to-lookup |
| Visual quality | Sheet charts | Smooth interactive canvas |
| Mobile/tablet | Barely works | Responsive design |
| Dark mode | Not possible | Supported |

---

## 9. Trade Suggestion Engine (NEW)

Auto-generated buy trade suggestions triggered when directional signal and tendency agree. Designed with strict thresholds because buy trades are inherently risky.

### Two Suggestion Types

| Type | Trigger Window | Sanity Check | SL | Target |
|------|---------------|--------------|-----|--------|
| **Overnight** | ~3:15 PM | Once at ~3:25 PM | 70% of entry | 150% of entry |
| **Intraday** | ~10:00 AM | Every 15 min until 2:00 PM | 75% of entry | 140% of entry |

### Trigger Thresholds

| Parameter | Overnight | Intraday |
|-----------|-----------|----------|
| Min factors agree | 6/8 | 6/8 |
| Min absolute score | 2.0 | 2.5 |
| Min confidence | 60% | 65% |
| Sanity min factors | 5/8 | 5/8 |

### Rules
- Tendency MUST agree with directional signal (strict filter)
- Suggestions persist as notifications until user dismisses, SL hits, or target is achieved
- Stale suggestions (>24h) are auto-expired
- Scheduler broadcasts `trade_suggestions` via WebSocket for real-time UI updates

---

## 10. Page Header Design (NEW)

Each page has a colored icon badge beside the page name:

| Page | Icon | Color |
|------|------|-------|
| Dashboard | Target circle | Blue (#2563EB) |
| Option Chain | Grid square | Red (#DC2626) |
| Positions | Box | Green (#16A34A) |
| Analytics | Pie chart | Purple (#7C3AED) |
| Journal | List | Indigo (#6366F1) |
| Alerts | Lightning | Amber (#D97706) |

---

## 11. Key Configuration

| Parameter | Value | Notes |
|-----------|-------|-------|
| NIFTY lot size | **65** | Updated as of April 2026. Do not change without verification. |
| Fast refresh | 15 seconds | Spot, VIX, LTP, P&L, risk scores |
| Slow refresh | 180 seconds | S/R levels, Fibonacci, Pivots |
| Market hours | Weekdays 9:00-15:45 IST | NSE APIs only hit during this window |
| VIX Offset default | 50% | Resets to 50 on each page load |

---

*This document serves as the product blueprint. The backend logic already exists in the sheet — the work is restructuring it into API endpoints and building the frontend to this spec.*
