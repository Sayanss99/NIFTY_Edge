"""FII/DII institutional-flow client — daily provisional figures.

India publishes no intraday FII/DII feed.  The numbers below are the standard
provisional figures NSE releases after the close (~6:30 PM IST) — the same ones
Moneycontrol / Sensibull quote:

  • Cash-segment net buy/sell for FII/FPI and DII — NSE ``fiidiiTradeReact`` API.
  • FII net **index-futures** positioning (Long − Short contracts) — NSE's
    participant-wise OI archive (``fao_participant_oi_<DDMMYYYY>.csv``).

Both refresh once per trading day; we cache for 30 min and degrade gracefully
(cash-only if the F&O archive for the day isn't published yet).  Pattern mirrors
sensibull.py / chartink.py — a Chrome-impersonated curl_cffi session, warmed up
against nseindia.com so the cookie-gated endpoints respond.
"""

from datetime import datetime, timedelta
from curl_cffi import requests as _cr

from market_data import _memo

_TTL = 1800.0  # 30 min — data is daily, no point refetching often
_CASH_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
_PART_OI_URL = (
    "https://archives.nseindia.com/content/nsccl/fao_participant_oi_{ddmmyyyy}.csv"
)


def _f(val) -> float:
    """Parse a numeric NSE field that may carry commas / blanks / '-'."""
    try:
        return float(str(val).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _session():
    """Chrome-impersonated session warmed up so NSE's cookie wall lets us in."""
    s = _cr.Session(impersonate="chrome")
    try:
        s.get("https://www.nseindia.com/", timeout=15)
        s.get("https://www.nseindia.com/reports/fii-dii", timeout=15)
    except Exception:
        pass
    return s


def _fetch_cash(s) -> dict:
    """FII & DII net cash flows (₹ cr) for the latest published session."""
    out = {}
    try:
        rows = s.get(_CASH_URL, timeout=15).json()
    except Exception as e:
        print(f"  [FII/DII] cash fetch failed: {e}")
        return out
    for r in (rows or []):
        cat = str(r.get("category", "")).upper()
        net = round(_f(r.get("netValue")), 2)
        if out.get("date") is None and r.get("date"):
            out["date"] = r.get("date")
        if "FII" in cat or "FPI" in cat:
            out["fii_cash_net"] = net
        elif "DII" in cat:
            out["dii_cash_net"] = net
    return out


def _fii_fut_legs_for(s, day):
    """FII Index-Futures (long, short) contracts from the participant-OI archive
    for a given date, or None if that day's file isn't published."""
    url = _PART_OI_URL.format(ddmmyyyy=day.strftime("%d%m%Y"))
    try:
        resp = s.get(url, headers={"referer": "https://www.nseindia.com/"}, timeout=20)
        if resp.status_code != 200 or not resp.text:
            return None
        lines = [ln for ln in resp.text.splitlines() if ln.strip()]
        # First line is a title ("Open Interest as on ..."); the real header
        # row starts with "Client Type".
        header_idx = next(
            (i for i, ln in enumerate(lines)
             if ln.lower().lstrip().startswith("client type")), None)
        if header_idx is None:
            return None
        cols = [c.strip() for c in lines[header_idx].split(",")]
        try:
            li = cols.index("Future Index Long")
            si = cols.index("Future Index Short")
        except ValueError:
            return None
        for ln in lines[header_idx + 1:]:
            parts = [p.strip() for p in ln.split(",")]
            if parts and parts[0].upper() == "FII":
                return _f(parts[li]), _f(parts[si])
    except Exception:
        return None
    return None


def _fut_buildup(d_long: float, d_short: float) -> tuple[str, str]:
    """Classify FII index-futures positioning from the day-over-day change in its
    long and short legs — the dominant (larger) leg move decides:

        long leg ↑   → Long build   (fresh longs, bullish)
        long leg ↓   → Long unwind  (longs cut, bearish)
        short leg ↑  → Short build  (fresh shorts, bearish)
        short leg ↓  → Short cover  (shorts covered, bullish)

    Returns (code, label)."""
    al, ash = abs(d_long), abs(d_short)
    if al == 0 and ash == 0:
        return "neutral", "No change"
    if al >= ash:
        return ("long_build", "Long build") if d_long > 0 else ("long_unwind", "Long unwind")
    return ("short_build", "Short build") if d_short > 0 else ("short_cover", "Short cover")


def _fetch_fii_fut(s) -> dict:
    """FII index-futures positioning, its day-over-day net CHANGE, a buildup read
    of WHICH leg moved, and a 3-day EMA of the daily net change.

    Finds the most recent participant-OI archives (walking back over weekends/
    holidays). ``fii_fut_chg`` = latest net − previous net (net = Long − Short).
    ``fii_fut_signal`` classifies the move (long_build / short_cover / short_build /
    long_unwind) from the long/short leg deltas. ``fii_fut_chg_ema3`` smooths the
    daily net change (EMA span 3) so a single expiry-day spike doesn't distort the
    market-tone verdict — needs up to 4 archives; degrades gracefully to fewer."""
    base = datetime.now()
    found = []   # [(date, long, short)] — newest first
    for back in range(0, 16):
        d = base - timedelta(days=back)
        legs = _fii_fut_legs_for(s, d)
        if legs is not None:
            found.append((d, legs[0], legs[1]))
            if len(found) >= 4:
                break
    if not found:
        print("  [FII/DII] participant OI archive unavailable")
        return {}
    latest_d, latest_l, latest_s = found[0]
    latest_net = latest_l - latest_s
    out = {"fii_fut_net": round(latest_net, 0),
           "fut_date": latest_d.strftime("%d-%b-%Y")}
    if len(found) >= 2:
        prev_d, prev_l, prev_s = found[1]
        prev_net = prev_l - prev_s
        d_long, d_short = latest_l - prev_l, latest_s - prev_s
        code, label = _fut_buildup(d_long, d_short)
        # Strength = how big the move is, as a % of the DOMINANT leg's prior size
        # (the leg that decided the verdict). e.g. shorts cut from 100k→75k ⇒
        # "Short Cover +25%". Scales the verdict from a faint nudge to a strong shift.
        if abs(d_long) >= abs(d_short):
            base_leg, leg_delta = prev_l, d_long
        else:
            base_leg, leg_delta = prev_s, d_short
        strength = round(abs(leg_delta) / base_leg * 100, 1) if base_leg else None
        out.update({
            "fii_fut_prev": round(prev_net, 0),
            "fii_fut_chg": round(latest_net - prev_net, 0),
            "fii_fut_long_chg": round(d_long, 0),
            "fii_fut_short_chg": round(d_short, 0),
            "fii_fut_signal": code,
            "fii_fut_label": label,
            "fii_fut_strength_pct": strength,
            "fut_prev_date": prev_d.strftime("%d-%b-%Y"),
        })
        # 3-day EMA of the daily net change — the market-tone verdict uses this
        # (not the raw 1-day Δ) so one expiry-day spike can't swing it. Daily
        # changes from the nets we found; EMA span 3 (α = 0.5), applied
        # oldest→newest. Falls back to the single-day Δ when only 2 archives exist.
        nets = [lg - sh for (_d, lg, sh) in found]            # newest first
        daily = [nets[i] - nets[i + 1] for i in range(len(nets) - 1)]
        if daily:
            alpha = 2 / (3 + 1)
            ema = None
            for chg in reversed(daily):                       # oldest → newest
                ema = chg if ema is None else (alpha * chg + (1 - alpha) * ema)
            out["fii_fut_chg_ema3"] = round(ema, 0)
    return out


def get_fii_dii() -> dict:
    """Latest provisional institutional flows (shared, cached 30 min).

    Returns ``{fii_cash_net, dii_cash_net, fii_fut_net, fii_fut_prev,
    fii_fut_chg, date, fut_date, fut_prev_date, provisional}`` — any field may be
    ``None`` if that source is unavailable.
    """
    def _producer():
        data = {"fii_cash_net": None, "dii_cash_net": None, "fii_fut_net": None,
                "fii_fut_prev": None, "fii_fut_chg": None, "fii_fut_signal": None,
                "fii_fut_label": None, "fii_fut_long_chg": None, "fii_fut_short_chg": None,
                "fii_fut_strength_pct": None, "fii_fut_chg_ema3": None,
                "date": None, "fut_date": None, "fut_prev_date": None,
                "provisional": True}
        s = _session()
        data.update(_fetch_cash(s))
        data.update(_fetch_fii_fut(s))
        # Only cache a result that carries at least one real leg; otherwise let
        # the next caller retry (a transient block shouldn't pin empties).
        got = (data.get("fii_cash_net") is not None
               or data.get("fii_fut_net") is not None)
        return data if got else {}
    return _memo("fii_dii_flows", _TTL, _producer)
