"""
Option Chain with Greeks — powered by PNSEA + Black-Scholes
Greeks are computed on the fly using each strike's implied volatility from NSE.
"""

import numpy as np
from scipy.stats import norm
from datetime import datetime, date
import pandas as pd
from pnsea import NSE


# ── Black-Scholes Greeks ──────────────────────────────────────────────────────

def _d1_d2(S, K, T, r, sigma):
    """Core Black-Scholes intermediates. Returns (d1, d2)."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None, None
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return d1, d2


def greeks(S, K, T, r, sigma, option_type="CE"):
    """
    Compute Black-Scholes Greeks for a single option.

    Parameters
    ----------
    S           : float  — underlying spot price
    K           : float  — strike price
    T           : float  — time to expiry in years
    r           : float  — risk-free rate (decimal, e.g. 0.065)
    sigma       : float  — implied volatility (decimal, e.g. 0.18)
    option_type : str    — "CE" (call) or "PE" (put)

    Returns
    -------
    dict with delta, gamma, theta (per day), vega (per 1% IV move), rho
    """
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if d1 is None:
        return {g: np.nan for g in ("delta", "gamma", "theta", "vega", "rho")}

    n_d1 = norm.pdf(d1)                   # standard normal PDF at d1
    disc  = np.exp(-r * T)

    gamma = n_d1 / (S * sigma * np.sqrt(T))
    vega  = S * n_d1 * np.sqrt(T) / 100   # per 1% change in IV

    if option_type == "CE":
        delta = norm.cdf(d1)
        theta = ((-S * n_d1 * sigma / (2 * np.sqrt(T)))
                 - r * K * disc * norm.cdf(d2)) / 365
        rho   = K * T * disc * norm.cdf(d2) / 100
    else:                                  # PE
        delta = norm.cdf(d1) - 1
        theta = ((-S * n_d1 * sigma / (2 * np.sqrt(T)))
                 + r * K * disc * norm.cdf(-d2)) / 365
        rho   = -K * T * disc * norm.cdf(-d2) / 100

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega":  round(vega,  4),
        "rho":   round(rho,   4),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def time_to_expiry(expiry_str: str) -> float:
    """
    Convert NSE expiry string like '06-Mar-2025' to years remaining.
    Uses calendar days / 365.
    """
    expiry_dt = datetime.strptime(expiry_str, "%d-%b-%Y").date()
    today     = date.today()
    days_left = (expiry_dt - today).days
    return max(days_left, 0) / 365.0


def build_chain(symbol: str = "NIFTY",
                expiry_date: str | None = None,
                risk_free_rate: float = 0.0525) -> pd.DataFrame:
    """
    Fetch option chain from NSE and append computed Greeks.

    Parameters
    ----------
    symbol          : index symbol, e.g. "NIFTY", "BANKNIFTY"
    expiry_date     : "06-Mar-2025" format — None means nearest expiry
    risk_free_rate  : annualised decimal (default 6.5% = RBI repo rate approx)

    Returns
    -------
    DataFrame with one row per (strike, CE/PE) and columns:
        Strike, Type, LTP, IV, OI, Volume, delta, gamma, theta, vega, rho
    """
    nse = NSE()

    kwargs = {}
    if expiry_date:
        kwargs["expiry_date"] = expiry_date

    chain_df, expiries, spot = nse.options.option_chain(symbol, **kwargs)

    # chain_df already has rows for both CE and PE — inspect columns:
    # typical columns from PNSEA: strikePrice, CE_*, PE_* or separate rows

    # PNSEA returns a flattened DataFrame; detect layout
    if "strikePrice" in chain_df.columns:
        rows = _process_wide(chain_df, spot, expiry_date or expiries[0],
                             risk_free_rate)
    else:
        rows = _process_long(chain_df, spot, expiry_date or expiries[0],
                             risk_free_rate)

    result = pd.DataFrame(rows)
    return result, expiries, spot


def _extract_option_row(strike, opt_type, ltp, iv_pct, oi, vol,
                        spot, expiry, r):
    """Build one output row with Greeks computed."""
    T     = time_to_expiry(expiry)
    sigma = iv_pct / 100.0          # NSE gives IV as percentage (e.g. 12.5)

    g = greeks(spot, strike, T, r, sigma, option_type=opt_type)
    return {
        "Strike":  int(round(strike)),   # always int — avoids float equality miss
        "Type":    opt_type,
        "LTP":     ltp,
        "IV_%":    iv_pct,
        "OI":      oi,
        "Volume":  vol,
        **g,
    }


def _process_wide(df, spot, expiry, r):
    """Handle wide format: strikePrice | CE_* | PE_* columns."""
    rows = []
    for _, row in df.iterrows():
        strike = row.get("strikePrice")

        for opt_type, prefix in (("CE", "CE_"), ("PE", "PE_")):
            ltp    = row.get(f"{prefix}lastPrice",        np.nan)
            iv_pct = row.get(f"{prefix}impliedVolatility", 0)
            oi     = row.get(f"{prefix}openInterest",     np.nan)
            vol    = row.get(f"{prefix}totalTradedVolume", np.nan)

            if pd.isna(ltp) or ltp == 0:
                continue

            rows.append(_extract_option_row(
                strike, opt_type, ltp, iv_pct or 0, oi, vol, spot, expiry, r
            ))
    return rows


def _process_long(df, spot, expiry, r):
    """Handle long / stacked format where each row is one option."""
    rows = []
    for _, row in df.iterrows():
        strike   = row.get("strikePrice") or row.get("Strike")
        opt_type = row.get("optionType") or row.get("Type", "CE")
        ltp      = row.get("lastPrice") or row.get("LTP", np.nan)
        iv_pct   = row.get("impliedVolatility") or row.get("IV", 0)
        oi       = row.get("openInterest") or row.get("OI", np.nan)
        vol      = row.get("totalTradedVolume") or row.get("Volume", np.nan)

        if pd.isna(ltp) or ltp == 0:
            continue

        rows.append(_extract_option_row(
            strike, opt_type, ltp, iv_pct or 0, oi, vol, spot, expiry, r
        ))
    return rows


# ── Main ─────────────────────────────────────────────────────────────────────

def parse_strikes(raw: list[str]) -> list[tuple[int, str]]:
    """
    Parse strike arguments of the form '22500:PE' or '24400:CE'.
    Returns list of (strike_int, type_str) tuples.
    """
    result = []
    for item in raw:
        try:
            parts = item.upper().split(":")
            if len(parts) != 2:
                raise ValueError
            strike    = int(parts[0])
            opt_type  = parts[1]
            if opt_type not in ("CE", "PE"):
                raise ValueError(f"Type must be CE or PE, got '{opt_type}'")
            result.append((strike, opt_type))
        except ValueError as e:
            raise SystemExit(f"[ERROR] Invalid strike format '{item}'. Use STRIKE:TYPE, e.g. 22500:PE\n{e}")
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch NSE option chain and print premium, IV, and delta for specific strikes.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--symbol", default="NIFTY",
        help="Index symbol  (default: NIFTY). E.g. BANKNIFTY, FINNIFTY",
    )
    parser.add_argument(
        "--expiry", default=None,
        help="Expiry date in DD-Mon-YYYY format.  E.g. 17-Mar-2026\n"
             "Omit to use the nearest expiry.",
    )
    parser.add_argument(
        "--strikes", nargs="+", default=None, metavar="STRIKE:TYPE",
        help="One or more strikes with type, e.g:  22500:PE  24400:CE\n"
             "Omit to show the 20 nearest ATM strikes.",
    )
    parser.add_argument(
        "--rate", type=float, default=0.065,
        help="Risk-free rate as decimal  (default: 0.065 = 6.5%%)",
    )
    args = parser.parse_args()

    print(f"Fetching {args.symbol} option chain …")
    df, expiries, spot = build_chain(args.symbol, args.expiry, args.rate)
    used_expiry = args.expiry or expiries[0]

    print(f"\nSpot : {spot}")
    print(f"Expiry used : {used_expiry}")
    print(f"Available expiries: {expiries}\n")

    if args.strikes:
        targets = parse_strikes(args.strikes)
        rows = []
        for strike, opt_type in targets:
            match = df[(df["Strike"] == strike) & (df["Type"] == opt_type)]
            if match.empty:
                print(f"[WARN] No data for {strike} {opt_type} — strike may not exist for this expiry.")
            else:
                rows.append(match.iloc[0])

        if rows:
            result = pd.DataFrame(rows)[["Strike", "Type", "LTP", "IV_%", "delta", "gamma", "theta", "vega"]]
            print(result.to_string(index=False))
    else:
        # Default: show 20 nearest ATM strikes
        atm = df.iloc[(df["Strike"] - spot).abs().argsort()].head(20)
        print(atm.to_string(index=False))
