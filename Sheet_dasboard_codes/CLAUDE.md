# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NIFTY options trading risk monitor that fetches live NSE option chain data, computes Black-Scholes Greeks, and syncs everything to a Google Sheet with P&L tracking, risk scoring, OI+Fibonacci S/R levels, and preventive hedge suggestions.

## Running

```bash
# Install dependencies (first time)
pip install pnsea gspread google-auth pandas numpy scipy yfinance

# Main monitor (runs continuously with 15s fast / ~3min slow cycle)
python risk_monitor.py

# Standalone option chain CLI
python option_chain_greeks.py --symbol NIFTY --expiry 17-Mar-2026 --strikes 22500:PE 24400:CE

# Legacy simple sync (not used in production)
python sheets_sync.py
```

## Architecture

- **`option_chain_greeks.py`** — Core engine. `build_chain(symbol, expiry, rate)` returns `(DataFrame, expiry_list, spot_price)`. Uses PNSEA library (`NSE()`) to fetch option chain, then computes Black-Scholes Greeks (`greeks()` function) for each strike using scipy. DataFrame columns: Strike, Type, LTP, IV_%, OI, Volume, delta, gamma, theta, vega, rho.

- **`risk_monitor.py`** — Main application. Multi-speed refresh loop:
  - Every 15s: spot price, VIX (B3-B6), current LTP & unrealized P&L per position
  - Every ~3min (slow cycle): risk scores (0-100), hedge trigger checks
  - At 10:00/12:30/15:30: daily IV & delta snapshots to date columns
  - At 15:15 weekdays: OI + Fibonacci S/R level refresh

  Key data flow: `read_config()` → `build_chain()` → `scan_pnl_table()` → compute LTP/P&L → `write_fast()`. Slow cycles add `compute_risk_score()` → `check_hedge_trigger()` → `compute_hedge()` → `write_risk()`/`write_hedge()`.

- **`sheets_sync.py`** — Simpler predecessor, writes LTP/IV/Delta for 4 strikes. Not used when risk_monitor.py is running.

- **`nseindia_scrapper.py`** — Legacy Selenium-based scraper (deprecated, replaced by PNSEA).

## Key Technical Details

- **PNSEA library**: NSE API wrapper with anti-blocking (stealthkit). Requires session warmup: call `nse.equity.market_status()` before other endpoints. Creates its own `NSE()` instance internally in `build_chain()`.
- **Google Sheets**: Uses gspread + service account (`credentials.json`). Sheet ID hardcoded in config. All writes use `ws.batch_update()` for efficiency. Cell layout is documented in the module docstring at the top of risk_monitor.py.
- **LOT_SIZE**: Hardcoded as 65 (NIFTY lot size). Not read from sheet.
- **P&L table**: Fixed structure at rows 19+ (header) / 20+ (data). Row deletion is avoided — closing a position clears cells A-I instead. TOTAL row is found dynamically by scanning column A.
- **Realized P&L**: Stored at O6:Q6 (headers) / O7+ (data). Accumulates by month+week. Must scan for first empty row carefully (gspread returns formatted empty rows).
- **Hedge lifecycle**: Script suggests → user picks Done/Rejected in M16/M17 → Done logs to P&L table via `insert_rows()`, Rejected clears suggestion.
- **Risk score weights**: delta(0.30) + IV(0.20) + premium(0.20) + proximity(0.25) + theta(0.05).
- **VIX change %**: NSE returns percentChange as whole number (e.g., -4.6 for -4.6%), must divide by 100 for sheet percentage formatting.

## Common Pitfalls

- NSE option chain returns 0 OI values after market hours — this is expected.
- `build_chain()` creates a fresh `NSE()` instance each call; VIX fetch uses the main loop's `nse` instance which needs periodic re-warming if session expires.
- Strike matching must use `int(round(strike))` — PNSEA sometimes returns float strikes.
- When positions are empty, risk/hedge cells must be cleared to avoid showing stale data.
