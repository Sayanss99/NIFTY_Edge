# Stratovex — NIFTY Edge Trading Workstation

Stratovex is a local-first web app for Indian derivatives traders. It leads with three core workflows:

- **Stock-option picking** — screens the F&O universe for high-conviction option-buying candidates, surfacing confirmed picks and an early-warning radar with live, tradeable suggestions.
- **NIFTY directional analysis** — reads live spot, volatility, ranges, and positioning to gauge the market's likely direction and conviction, with expected-move ranges and a composite market verdict.
- **NIFTY strategy builder** — assembles and evaluates multi-leg option strategies with payoff previews, strategy rankings, and margin estimates so setups can be compared before they are booked.

It then rounds this out with live option-chain analytics, position tracking, risk scoring, hedge/roll ideas, alerts, news context, futures hedging, journaling, and performance analytics — all in one authenticated dashboard.

The project is intentionally lightweight on the frontend: FastAPI serves a vanilla HTML/CSS/JS app, while the backend handles NSE data fetches, SQLite persistence, background refresh jobs, and WebSocket broadcasts.

> This is an analytics and journaling tool, not an order-routing platform. Trade booking, hedge execution, rolls, and suggestions are recorded inside the app; they do not place broker orders.

## What The App Does

- Authenticated login/signup with 7-day JWT sessions, profile editing, persisted user settings, and localStorage-backed session state.
- First-run onboarding tour, app shell header/sidebar generation, profile card, alert-count badge, and page-wide theme/tweak controls.
- Live NIFTY dashboard with spot, VIX, days to expiry, expected ranges, skew/tendency, max pain, IV rank/percentile, 18-parameter market verdict, news tone, margin summary, EOD summary, and active trade signals.
- Dashboard range/spot monitor with an IND/SYM toggle to switch the chart between the NIFTY index and a stock symbol (it auto-follows your latest trade's underlying, and the toggle always mirrors the chart). NIFTY view keeps calculated range, VIX-offset range, max pain, S/R overlays, position markers, layer toggles, Y zoom, and selected-expiry recalculation; symbol view shows that stock's spot, OI-based S/R, max pain, and your positions, with the Support & Resistance card and positions summary following the selected symbol.
- Live NIFTY and stock option chains with an IND/SYM toggle, expiry selection, ATM highlighting, CE/PE strike lookup, premium ranking, lot-size awareness, risk reversal, and session-persisted symbol search.
- Open-position tracker for NIFTY options, stock options, and NIFTY futures hedges with P&L, IV change, greeks, risk score, hedge suggestions, roll scenarios, partial close, and auto-expiry cleanup.
- Position sizing helper for risk-based lot calculation, stop-loss percentage, break-even preview, and automatic quantity application.
- Daily loss-stop guard that warns/soft-blocks new trade booking after a configured realized-loss threshold, with explicit override support.
- Hedge choices for short NIFTY option positions: same-expiry buy protection or next-expiry protective short where available.
- Roll engine for short NIFTY option trades: OTM roll, calendar roll, and futures-delta hedge scenarios with recommendation scoring and confirmation flow.
- Trade journal with closed/expired trades, summary stats, CSV export, trade notes, and mistake-tag annotations.
- Analytics dashboard with cumulative P&L, time filters, win/loss donut, a Global⇄Symbol scope toggle (Performance + Strike Patterns scope to a chosen underlying; Signals stay NIFTY-wide), a P&L-by-underlying heatmap (click to scope), CE-vs-PE-per-underlying view, strike patterns, mistake breakdown, EOD summary (with per-trade symbol), signal history, and signal accuracy.
- Strategy builder with multi-leg strategy creation, payoff preview, payoff persistence, closed-strategy view, market analytics, strategy rankings, and margin estimation.
- Margin calculator with strategy presets, long-only debit handling, scenario-style SPAN approximation, exposure margin, premium credit/debit, effective cost, expiry-day margin, and ROI estimate.
- Alert center for price, VIX, risk, premium, IV, delta, and range-breach rules with per-position targeting, stock-symbol filtering, cooldowns, pause/resume, delete, history, unread counts, and WebSocket notifications.
- Trade suggestion center for active/history NIFTY signals, with execute/reject actions and scheduler-managed overnight/intraday signal lifecycle.
- News Desk that polls RSS/GNews sources, deduplicates articles, scores impact/direction/category, exposes dashboard ticker context, supports filters/pagination/detail expansion, and per-user dismissals.
- Futures desk for NIFTY futures contracts, manual futures positions, margin estimate, futures close, and futures-based hedge placement with basis sanity checks.
- Stock Options page for F&O screening using RSI, DeMark TD setup, IV percentile/HV proxy, synthetic forward-vs-spot read, delta band, premium yield, radar/confirmed buckets, refresh, and live suggestion confirmation.
- Live UI layer with WebSocket reconnects, market/chain/position/alert/news events, number-change pulses, local sparklines, and responsive sizing/layout helpers.
- Consistent frontend resilience: shared error/empty-state renderers with retry, unified toast handling, visibility-aware polling that pauses on hidden tabs, debounced canvas redraw on resize/theme change, and a synced dark-mode toggle across the profile switch and Tweaks panel.
- Visual option-chain OI magnitude bars and a hover tooltip on the dashboard range/spot monitor.
- Operational backend layer: `/health` endpoint, rotating file + console logging, and a daily maintenance job (retention pruning, weekly VACUUM, rotating SQLite backups).
- SQLite persistence with lightweight startup migrations, IV history bootstrap, EOD signal capture, IV term-structure capture, skew history, stock IV history, news dismissal, trade annotations, and daily EOD summaries.

## Project Structure

```text
NIFTY_Edge/
├── backend/
│   ├── main.py                    # FastAPI entry point, logging setup, /health, route registration, frontend serving, WebSocket auth
│   ├── database.py                # SQLite engine, session factory, create_all, lightweight migrations, IV bootstrap
│   ├── models.py                  # SQLAlchemy models for users, trades, alerts, strategies, news, IV history, etc.
│   ├── auth.py                    # JWT auth and bcrypt password hashing
│   ├── config.py                  # Runtime constants: JWT secret, NIFTY lot size, refresh cadence, news thresholds
│   ├── market_data.py             # NSE option-chain fetches, greeks, ranges, max pain, S/R, risk, margin helpers
│   ├── scheduler.py               # Background market/news/screener refresh loop, WebSocket broadcasts, daily maintenance trigger
│   ├── maintenance.py             # Daily DB retention pruning, weekly VACUUM, rotating SQLite backups
│   ├── ws_manager.py              # Per-user WebSocket connection manager
│   ├── alert_engine.py            # Alert evaluation logic
│   ├── trade_suggestion_engine.py # Intraday/overnight NIFTY option suggestion lifecycle
│   ├── nse_futures.py             # NIFTY futures feed, basis checks, contract helpers
│   ├── stock_screener.py          # F&O stock screener pipeline and live sell-suggestion builder
│   ├── sensibull.py               # Sensibull-backed stock IV percentile helpers
│   ├── chartink.py                # Chartink-related helper layer
│   ├── news_sources.py            # News source registry
│   ├── news_entities.py           # News scoring keywords/categories/config
│   ├── news_scorer.py             # Rule-based headline scorer
│   ├── news_scraper.py            # News fetch/dedupe/score/store workflow
│   ├── routes_auth.py             # /api/auth/*
│   ├── routes_market.py           # /api/market/*
│   ├── routes_trades.py           # /api/trades/*
│   ├── routes_analytics.py        # /api/analytics/*
│   ├── routes_alerts.py           # /api/alerts/*
│   ├── routes_suggestions.py      # /api/suggestions/*
│   ├── routes_strategies.py       # /api/strategies/*
│   ├── routes_news.py             # /api/news/*
│   ├── routes_futures.py          # /api/futures/*
│   ├── routes_stocks.py           # /api/stocks/*
│   ├── requirements.txt
│   ├── niftyedge.db               # Local SQLite DB, created/migrated by the app
│   ├── logs/                      # Rotating application log (niftyedge.log), created at startup
│   └── _backups/                  # Daily rotating SQLite backups, created by the maintenance job
│
├── frontend/
│   ├── index.html                 # Redirects to /dashboard or /login based on localStorage token
│   ├── favicon.png / favicon.svg
│   ├── css/styles.css             # App shell, dark UI, responsive pages, components
│   ├── js/
│   │   ├── api.js                 # Fetch wrapper, auth header injection, HTTP-error surfacing, domain API helpers
│   │   ├── ws.js                  # Authenticated WebSocket client with reconnect/event dispatch
│   │   ├── utils.js               # Header/sidebar/profile/tour/formatting, error/empty states, visibility-aware polling, resize redraw
│   │   ├── tweaks.js              # Theme/accent/density/texture/radius preferences (synced with the profile dark-mode toggle)
│   │   ├── sizing.js              # Risk-based lot sizing modal
│   │   ├── ne-live.js             # Live number pulses, sparklines, and row hover polish
│   │   └── ne-scroll.js           # Scroll-reveal and living-terminal enhancement layer
│   └── pages/
│       ├── login.html
│       ├── dashboard.html
│       ├── option_chain.html
│       ├── positions.html
│       ├── journal.html
│       ├── analytics.html
│       ├── alerts.html
│       ├── strategy_builder.html
│       ├── news_desk.html
│       ├── futures.html
│       └── stock_options.html
│
├── NiftyEdge_App_Plan.md          # Original/planning document
└── README.md
```

## Tech Stack

| Layer | Technology |
| --- | --- |
| Backend | Python, FastAPI, Uvicorn |
| Database | SQLite with SQLAlchemy ORM |
| Auth | JWT via python-jose, bcrypt password hashing |
| Market Data | `pnsea`, NSE helpers, `curl_cffi`, `yfinance`, custom futures/option-chain utilities |
| Scheduling | Async background loop started from FastAPI lifespan |
| Realtime | FastAPI WebSocket endpoint plus custom frontend client |
| Frontend | Vanilla HTML, CSS, and JavaScript |
| Charts/UI | Custom canvas/SVG/DOM rendering in page scripts |
| News | `feedparser`, `gnews`, rule-based scoring |
| Quant Helpers | NumPy, SciPy, pandas |

## Setup

### 1. Create And Activate A Virtual Environment

From the repository root:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install Dependencies

```powershell
pip install -r requirements.txt
```

### 3. Configure Secrets

For local development the app has a fallback JWT secret, but you should set your own secret before running anything serious. The app logs a warning on startup whenever the built-in dev secret is still in use:

```powershell
$env:NIFTYEDGE_SECRET_KEY="replace-this-with-a-long-random-secret"
```

Other optional environment overrides (all have sensible defaults):

```powershell
$env:NIFTYEDGE_DATABASE_URL="sqlite:///./niftyedge.db"   # SQLite location
$env:NIFTYEDGE_LOG_LEVEL="INFO"                          # DEBUG/INFO/WARNING/ERROR
$env:NIFTYEDGE_FAST_REFRESH_SECONDS="15"                 # live refresh cadence
$env:NIFTYEDGE_SLOW_REFRESH_MULTIPLIER="12"              # heavy-cycle multiplier
```

### 4. Run The Backend

Run from the `backend` directory. The static frontend paths in `main.py` are relative to this directory.

```powershell
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Then open:

```text
http://localhost:8000
```

The root page redirects to `/login` if no token exists or `/dashboard` if the browser already has a `niftyedge_token` in localStorage.

## Runtime Behavior

On startup, `main.py` configures logging (rotating file at `backend/logs/niftyedge.log` plus console), initializes the SQLite database, runs lightweight migrations in `database.py`, and starts the scheduler. An unauthenticated `/health` endpoint reports database reachability, the age of the last scheduler cycle, connected WebSocket clients, and whether the market is currently open.

The scheduler:

- Seeds a fresh market snapshot if the latest snapshot is missing or stale.
- Checks NSE market hours using IST and a 2026 holiday list. Confirmed holidays hard-close the market; tentative (moon-sighting-dependent) festival dates never force a close and are only flagged, so a wrong tentative guess cannot black out a live trading day.
- During market hours, refreshes NIFTY data every `FAST_REFRESH_SECONDS` seconds, currently 15 seconds.
- Runs slower support/resistance and heavier computations every `SLOW_REFRESH_MULTIPLIER` cycles, currently about 3 minutes.
- Polls news continuously about every 90 seconds when `NEWS_DESK_ENABLED` is true.
- Refreshes the stock-options prefilter about every 10 minutes.
- Broadcasts market, chain, S/R, risk-reversal, position, alert, news, and suggestion events over `/ws`.
- Manages overnight and intraday trade-suggestion windows, sanity checks, stale expiry, and live suggestion prices.
- Captures EOD signal snapshots, next-day signal correctness, ATM IV, IV term structure, 25-delta skew, and per-user EOD summaries.
- Auto-expires past option trades (rate-limited to once per minute per user) and promotes open child hedges so they remain visible.
- Runs a once-daily maintenance pass (`maintenance.py`): prunes old snapshots/S-R/news/alert rows past their retention windows, VACUUMs weekly, and writes a rotating daily SQLite backup into `_backups/`.

## Key Pages

### Login / App Shell

`login.html` handles signup/login and stores token/user data in localStorage. `utils.js` builds the reusable header/sidebar, profile card, alert count, and onboarding tour. `tweaks.js` adds persisted controls for theme, accent hue, density, texture, and card radius.

### Dashboard

The dashboard is the main trading cockpit. It shows live NIFTY spot/VIX, expected ranges, max pain, 25-delta risk reversal, IV rank/percentile, 18-parameter market verdict, news tone, scrolling high-impact news, open positions, margin summary, EOD summary, and active trade suggestions. It also supports selected-expiry recomputation, VIX-offset range control, Y zoom, chart layer toggles, a hover tooltip on the range/spot monitor, and stock-symbol market lookup.

### Option Chain

The option-chain page fetches live NIFTY or stock option-chain data, mirrors CE/PE rows around strike, highlights ATM, draws inline OI magnitude bars behind the CE/PE OI cells, supports expiry switching, and exposes strike lookup cards with LTP, IV, delta, gamma, theta, OI, volume, premium rank, selected symbol, and lot size.

### Positions

Positions tracks open option and futures legs. It supports manual trade entry, stock-option symbol and lot-size capture, parent/hedge linking, position sizing, full/partial close, P&L preview, IV change, position greeks, short-option risk scores, same-expiry and next-expiry hedge choices, roll scenarios, futures-delta hedges, margin calculator, and daily-loss override confirmation.

### Journal

The journal lists closed and expired trades, shows aggregate P&L stats, exports CSV, and lets users attach notes and mistake tags. Annotations are stored separately from trade lifecycle data so closed trades can remain immutable while notes evolve.

### Analytics

Analytics aggregates trade performance, strike patterns, mistake breakdown, EOD summary data, directional-signal history, and signal accuracy. It includes symbol-scoped views, time filters, cumulative P&L, expiry comparisons, win/loss donut, and mistake-tag breakdowns.

### Strategy Builder

The strategy builder supports multi-leg option strategies, payoff preview, strategy persistence, closed-strategy review, strategy close, market analytics, rankings, and margin estimation. Market analytics are shared with the dashboard so the verdict surfaces do not diverge.

### Alerts

Alerts are user-defined rules evaluated on market updates. Supported flows include create/update/delete, pause/resume, history, unread count, per-position rules, stock-symbol filtering for trade selection, cooldowns, and notification through in-app/WebSocket events. The Alerts page also hosts active and historical trade suggestions with execute/reject actions.

### News Desk

News Desk polls configured sources, deduplicates items, scores market impact, assigns direction/category, and stores the result. The page supports tier/direction/category/hour filters, pagination, expandable detail panels, matched-keyword display, article links, stats cards, and dismissals. The dashboard consumes recent high-impact items for ticker and context cards.

### Futures

The futures page works with NIFTY futures contracts, manual futures entries, futures positions, margin estimate, futures close, and futures-based hedge placement. The futures feed performs sanity checks around basis, lot size, and contract data before booking hedges.

### Stock Options

The stock-options page screens F&O stocks with a layered pipeline:

1. Multi-timeframe RSI gate.
2. Daily DeMark TD buy-setup gate.
3. User-adjustable IVP, premium-yield, and delta-band filters.

Confirmed names can generate live stock-option sell suggestions. Radar names surface DeMark 7/8 setups before full confirmation. The page also supports F&O symbol lookup through `/api/stocks/fno-list`, manual prefilter refresh, and stock chain metrics through `/api/market/*?symbol=...`.

## Backend Feature Map

| Area | Details |
| --- | --- |
| Auth | Signup, login, `/me`, settings, profile update, JWT validation for REST and WebSocket |
| Trades | NIFTY/stock options, futures legs, parent hedges, partial close, realized/unrealized P&L, auto-expiry, annotations, margin summary |
| Risk | Short-option risk scoring, portfolio greeks, hard hedge triggers, long-option max-loss treatment |
| Hedge/Roll | Buy-protection hedge, next-expiry protective short, OTM roll, calendar roll, futures-delta hedge |
| Market | Expiries, active-expiry selection, option chain, strike lookup, S/R, range computation, max pain, RR, tendency, directional signal, IV rank |
| Strategy | Market analytics, strategy rankings, CRUD, payoff preview, payoff persistence, strategy close |
| Alerts | Rules, cooldowns, history, unread count, WebSocket trigger events |
| Suggestions | Overnight/intraday suggestions, active/history views, execute/reject, stale expiry, sanity checks |
| News | RSS/GNews ingestion, dedupe, scoring, ticker, recent context, stats, user dismissals |
| Futures | Contract list, positions, manual place, hedge place, close, margin estimate |
| Stocks | F&O universe, screener cache, live suggestion builder, manual refresh |
| Data History | Daily signal snapshots, IV daily records, IV term structure, skew daily records, stock IV history, EOD summaries |

## API Overview

| Prefix | Purpose |
| --- | --- |
| `/api/auth` | Signup, login, current user, profile, settings |
| `/api/market` | Market status, snapshots, expiries, option chain, strike lookup, S/R, IV rank, margin, symbol compute |
| `/api/trades` | Trade CRUD, close/partial close, risk, journal summary/export, annotations, margin summary, hedge/roll flows |
| `/api/analytics` | Performance, strike patterns, mistakes, EOD summary, signal history and accuracy |
| `/api/alerts` | Alert rules, toggle, delete, history, unread count |
| `/api/suggestions` | Active/history trade suggestions, execute, reject |
| `/api/strategies` | Market analytics, rankings, strategy CRUD, payoff, close |
| `/api/news` | Feed, ticker, recent context, dismiss, stats |
| `/api/futures` | Contracts, positions, place, hedge, close, margin estimate |
| `/api/stocks` | F&O universe, stock screener, live suggestion builder, prefilter refresh |
| `/health` | Unauthenticated health check: DB reachability, last-cycle age, WebSocket client count, market-open state |
| `/ws` | Authenticated WebSocket endpoint, token passed as query parameter |

## Data And Persistence

- The app stores all local state in `backend/niftyedge.db`.
- `database.py` uses `Base.metadata.create_all()` plus explicit SQLite migrations for new columns/tables.
- There is no Alembic migration setup yet.
- Existing DBs are patched in place on startup for the currently known schema additions.
- IV rank history is bootstrapped from snapshots and synthetic proxy data when there is insufficient real history.
- Trade annotations, news dismissals, stock IV history, IV term structure, skew records, and daily EOD summaries are first-class persisted data.
- A daily maintenance job prunes high-volume tables past their retention windows (market snapshots ~30 days, S/R levels ~14 days, news ~30 days, alert logs ~90 days), VACUUMs weekly, and keeps a rotating set of daily DB backups in `backend/_backups/`.

## Configuration Notes

Important constants live in `backend/config.py`, several of which can be overridden by environment variables:

- `NIFTYEDGE_SECRET_KEY`: environment variable used for JWT signing; a startup warning is logged if the built-in dev secret is still in use.
- `NIFTYEDGE_DATABASE_URL`: SQLite location (`database.py`), defaults to `sqlite:///./niftyedge.db`.
- `NIFTYEDGE_LOG_LEVEL`: log verbosity (`main.py`), defaults to `INFO`.
- `LOT_SIZE`: NIFTY lot size, currently fixed at 65 in this codebase.
- `FAST_REFRESH_SECONDS`: live refresh cadence, currently 15 seconds (env: `NIFTYEDGE_FAST_REFRESH_SECONDS`).
- `SLOW_REFRESH_MULTIPLIER`: heavier computation cadence multiplier (env: `NIFTYEDGE_SLOW_REFRESH_MULTIPLIER`).
- `NEWS_DESK_ENABLED`: enables/disables news polling.
- `NEWS_TIER_*`: impact-score thresholds for headline tiers.
- Retention windows and backup rotation are defined in `backend/maintenance.py`.

## Development Notes

- Start the app from `backend`; relative static paths expect `../frontend/...`.
- Most frontend API calls go through `frontend/js/api.js`, which surfaces server (5xx) failures as thrown errors so pages can render a retryable error state instead of a frozen "Loading…". Client validation (4xx) responses are still returned to callers unchanged.
- Auth tokens are stored in browser localStorage as `niftyedge_token`.
- Runtime activity and failures are written to `backend/logs/niftyedge.log` (rotating) as well as the console; raise verbosity with `NIFTYEDGE_LOG_LEVEL`.
- The app relies on external market/news providers. NSE, yfinance, RSS, GNews, Sensibull, and pnsea availability can affect runtime behavior. NSE chain fetches retry with exponential backoff and jitter, then fall back to the last cached chain.
- Several modules contain runtime fallbacks and sanity checks, but stale/missing external data can still surface as empty states.

## Known Limitations

- No broker integration and no real order placement.
- No formal Alembic migrations.
- No test suite is currently present in the repository.
- NSE holidays are hard-coded for 2026 and should be reviewed annually. Tentative festival dates are tracked separately from confirmed ones and must be promoted to the confirmed list once the official NSE circular fixes them.
- No CORS or rate-limiting middleware is configured yet; intended for local/trusted use.
- Stock-option roll/hedge logic is deferred; roll scenarios currently guard against non-NIFTY stock-option trades.
- News scoring is rules-based and should be treated as contextual signal, not investment advice.

## Quick Run Checklist

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:NIFTYEDGE_SECRET_KEY="replace-this-with-a-long-random-secret"
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`, create an account, and let the scheduler seed the first snapshot.