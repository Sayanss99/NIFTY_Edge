"""
sheets_sync.py — NSE Option Data (LTP, IV%, Delta) for 4 strikes into Google Sheets

Reads symbol, expiry, and 4 strike prices from the sheet, fetches live NSE data
in a single API call, and writes LTP / IV_% / Delta back to the respective rows.

Sheet layout:
  Inputs (you fill):
    A3       Symbol   e.g. NIFTY
    B2       Expiry   e.g. 17-Mar-2026
    B11      CE strike 1  (e.g. NIFTY_Upside)
    B12      PE strike 1  (e.g. NIFTY_Downside)
    B13      CE strike 2  (e.g. NIFTY_MAX_LIMIT)
    B14      PE strike 2  (e.g. NIFTY_MIN_LIMIT)
    B18      CE type label  (must contain "CE")
    B19      PE type label  (must contain "PE")

  Outputs (script fills):
    C11  D11  E11   → LTP, IV_%, Delta  for B11 CE
    C12  D12  E12   → LTP, IV_%, Delta  for B12 PE
    C13  D13  E13   → LTP, IV_%, Delta  for B13 CE
    C14  D14  E14   → LTP, IV_%, Delta  for B14 PE
"""

import sys
import time
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from option_chain_greeks import build_chain

# ── CONFIG (edit these once) ──────────────────────────────────────────────────

SERVICE_ACCOUNT_FILE = "credentials.json"
SHEET_ID             = "1evL0PFhsHY4Yo7nlV6oRa0DYyPM_ykJkdbBlrYnuYr0"
WORKSHEET_NAME       = "Sheet1"
RISK_FREE_RATE       = 0.0525

# Input cells
CELL_SYMBOL = "A3"
CELL_EXPIRY = "B2"
CELL_CE_TYPE = "B18"
CELL_PE_TYPE = "B19"

# Strike input cell → (option type, output cells: LTP, IV_%, Delta)
STRIKES = {
    "B11": ("CE", "C11", "D11", "E11"),
    "B12": ("PE", "C12", "D12", "E12"),
    "B13": ("CE", "C13", "D13", "E13"),
    "B14": ("PE", "C14", "D14", "E14"),
}

# ── Auth ──────────────────────────────────────────────────────────────────────

def connect_sheet() -> gspread.Worksheet:
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds).open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Connecting to Google Sheets ...")
    ws = connect_sheet()

    # 1. Read all inputs in one batch
    all_input_cells = [CELL_SYMBOL, CELL_EXPIRY] + list(STRIKES.keys())
    raw = ws.batch_get(all_input_cells)

    def cell_val(i):
        try:
            return raw[i][0][0]
        except (IndexError, TypeError):
            return ""

    symbol = cell_val(0)
    expiry = cell_val(1)
    strike_raws = {cell: cell_val(i + 2) for i, cell in enumerate(STRIKES.keys())}

    # 2. Validate
    if not symbol or not expiry:
        sys.exit(f"[ERROR] Symbol (A3) or Expiry (B2) is empty.")

    strikes_parsed = {}
    for cell, raw_val in strike_raws.items():
        if not raw_val:
            sys.exit(f"[ERROR] Strike cell {cell} is empty.")
        try:
            strikes_parsed[cell] = int(str(raw_val).replace(",", "").strip())
        except ValueError:
            sys.exit(f"[ERROR] Strike cell {cell} must be a number, got '{raw_val}'")

    print(f"Symbol: {symbol}  |  Expiry: {expiry}")
    for cell, strike in strikes_parsed.items():
        opt_type = STRIKES[cell][0]
        print(f"  {cell} = {strike} {opt_type}")

    # 3. Fetch option chain once (covers all 4 strikes)
    print(f"\nFetching {symbol} option chain ...")
    df, _, spot = build_chain(symbol, expiry, RISK_FREE_RATE)
    print(f"Spot: {spot}\n")

    # 4. Build batch update — one call to write all outputs
    updates = []
    for strike_cell, (opt_type, ltp_cell, iv_cell, delta_cell) in STRIKES.items():
        strike = strikes_parsed[strike_cell]
        match = df[(df["Strike"] == strike) & (df["Type"] == opt_type)]

        if match.empty:
            print(f"  [WARN] No data for {strike} {opt_type} — skipping row.")
            continue

        row  = match.iloc[0]
        ltp   = round(float(row["LTP"]),   2)
        iv    = round(float(row["IV_%"]),  2)
        delta = round(float(row["delta"]), 4)

        updates += [
            {"range": ltp_cell,   "values": [[ltp]]},
            {"range": iv_cell,    "values": [[iv]]},
            {"range": delta_cell, "values": [[delta]]},
        ]
        print(f"  {strike} {opt_type}  ->  LTP: {ltp:>8.2f}  IV: {iv:>5.1f}%  Delta: {delta:>7.4f}")

    # 5. Write all in one API call
    if updates:
        ws.batch_update(updates)
        print(f"\nDone. {len(updates) // 3} strikes written to sheet.")
    else:
        print("\n[WARN] Nothing written — no matching strikes found.")


REFRESH_INTERVAL = 3 * 60   # seconds — change this to adjust refresh rate

if __name__ == "__main__":
    print(f"Starting live refresh every {REFRESH_INTERVAL // 60} min. Press Ctrl+C to stop.\n")
    while True:
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Refreshing ...")
            main()
        except KeyboardInterrupt:
            print("\nStopped.")
            sys.exit(0)
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e} — retrying next cycle.")
        time.sleep(REFRESH_INTERVAL)
