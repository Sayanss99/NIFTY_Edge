"""Standalone market-news fetcher for NiftyEdge.

Uses the `gnews` package to fetch domestic and global headlines, filters them
for stock-market relevance, and prints both structured JSON and a ticker-ready
text line. This is intentionally standalone so it can be tested before any UI
integration.

Examples:
    python backend/news_market_fetcher.py
    python backend/news_market_fetcher.py --domestic-limit 8 --global-limit 8
    python backend/news_market_fetcher.py --json-only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, asdict
from email.utils import parsedate_to_datetime
from typing import Iterable


try:
    from gnews import GNews
except ImportError:  # pragma: no cover - handled at runtime
    GNews = None


DOMESTIC_QUERIES = [
    "Nifty",
    "Sensex",
    "Gift Nifty",
    "Bank Nifty",
    "Nifty IT",
    "Nifty Auto",
    "Nifty Pharma",
    "Nifty FMCG",
    "Nifty Realty",
    "Nifty PSU Bank",
    "Indian stock market",
    "India markets",
    "NSE stocks",
    "banking stocks India",
    "IT stocks India",
    "auto stocks India",
    "pharma stocks India",
    "energy stocks India",
    "metal stocks India",
    "capital goods stocks India",
    "FII DII India market",
    "Reliance Industries",
    "HDFC Bank",
    "ICICI Bank",
    "State Bank of India",
    "Infosys",
    "TCS",
    "Bharti Airtel",
    "Larsen and Toubro",
    "Adani group stocks",
]

GLOBAL_QUERIES = [
    "USD INR",
    "rupee dollar exchange rate",
    "Federal Reserve",
    "Fed meeting",
    "Fed rate cut",
    "RBI MPC repo rate",
    "repo rate India",
    "Brent crude",
    "oil prices",
    "OPEC",
    "Middle East tensions oil",
    "US recession inflation",
    "bond yields dollar",
    "China economy global trade",
]

MARKET_TERMS = {
    # General market
    "market": 2,
    "markets": 2,
    "stock": 3,
    "stocks": 3,
    "shares": 3,
    "equity": 3,
    "equities": 3,
    "benchmark": 2,
    "blue chip": 2,
    "largecap": 2,
    "midcap": 2,
    "smallcap": 2,
    "trading": 2,
    "investor": 1,
    "investors": 1,
    "brokerage": 2,
    "target price": 2,
    "buy call": 2,
    "sell call": 2,
    "outperform": 2,
    "underperform": 2,
    "overweight": 2,
    "underweight": 2,
    "accumulate": 2,
    "downgrade": 2,
    "upgrade": 2,
    "valuation": 2,
    "multiple": 1,
    "pe ratio": 1,
    "ebitda": 1,
    "guidance": 2,
    "margin": 1,
    "revenue": 1,
    "profit": 1,
    "net profit": 2,
    "quarterly results": 3,
    "q1": 1,
    "q2": 1,
    "q3": 1,
    "q4": 1,
    "earnings": 3,
    "results": 2,
    "dividend": 3,
    "bonus issue": 3,
    "stock split": 3,
    "rights issue": 3,
    "buyback": 3,
    "block deal": 3,
    "bulk deal": 3,
    "stake sale": 2,
    "open offer": 3,
    "merger": 2,
    "acquisition": 2,
    "order win": 3,
    "order book": 2,
    "capex": 2,
    "ipo": 3,
    "listing": 2,
    "fpo": 2,
    "qip": 2,
    "delisting": 3,
    # India indices and domestic cues
    "nifty": 5,
    "gift nifty": 7,
    "bank nifty": 6,
    "finnifty": 5,
    "sensex": 5,
    "nse": 4,
    "bse": 4,
    "sgx nifty": 5,
    "gift city": 4,
    "fii": 4,
    "dii": 4,
    "foreign institutional": 3,
    "domestic institutional": 3,
    "advance decline": 2,
    "market breadth": 2,
    "rollover": 2,
    "derivatives": 2,
    "futures": 2,
    "options": 2,
    "open interest": 3,
    "oi": 2,
    "put call ratio": 3,
    "pcr": 2,
    "india vix": 4,
    "vix": 2,
    "support": 1,
    "resistance": 1,
    "breakout": 2,
    "breakdown": 2,
    # Macro and rates
    "fed": 4,
    "federal reserve": 5,
    "fed meeting": 6,
    "fomc": 5,
    "rate cut": 4,
    "rate hike": 4,
    "interest rate": 3,
    "repo rate": 7,
    "rbi": 6,
    "mpc": 5,
    "monetary policy": 4,
    "bond yield": 3,
    "treasury yield": 3,
    "inflation": 2,
    "cpi": 2,
    "wpi": 2,
    "gdp": 2,
    "iip": 2,
    "pmi": 2,
    # FX, commodities, energy
    "usd inr": 7,
    "inr usd": 6,
    "rupee": 3,
    "dollar": 2,
    "dollar index": 2,
    "forex": 2,
    "currency": 1,
    "exchange rate": 2,
    "brent": 6,
    "brent crude": 7,
    "crude": 4,
    "crude oil": 5,
    "oil prices": 4,
    "opec": 4,
    "gas prices": 2,
    "gold prices": 2,
    # Global situation
    "tariff": 2,
    "trade war": 3,
    "geopolitical": 3,
    "middle east": 3,
    "russia": 1,
    "ukraine": 1,
    "china": 1,
    "global growth": 2,
    "recession": 3,
    # Sectors
    "banking": 3,
    "financials": 3,
    "it sector": 3,
    "technology sector": 2,
    "auto sector": 3,
    "pharma sector": 3,
    "fmcg": 3,
    "realty": 3,
    "infra": 3,
    "infrastructure": 3,
    "capital goods": 3,
    "metals": 3,
    "metal stocks": 3,
    "energy sector": 3,
    "oil marketing companies": 3,
    "omc": 2,
    "telecom": 3,
    "defence": 3,
    "railway": 2,
    "psu": 3,
    "psu bank": 4,
    # Major Indian names
    "reliance": 3,
    "hdfc bank": 4,
    "icici bank": 4,
    "sbi": 4,
    "state bank of india": 4,
    "infosys": 4,
    "tcs": 4,
    "tata consultancy services": 4,
    "larsen and toubro": 4,
    "bharti airtel": 4,
    "adani": 4,
    "adani ports": 4,
    "adani enterprises": 4,
    "asian paints": 3,
    "bajaj finance": 4,
    "kotak mahindra bank": 4,
    "axis bank": 4,
    "sun pharma": 4,
    "dr reddy": 3,
    "maruti": 4,
    "mahindra": 3,
    "tata motors": 4,
    "ultratech": 3,
    "hindalco": 3,
    "jsw steel": 3,
    "ntpc": 3,
    "power grid": 3,
    "coal india": 3,
}

NOISE_TERMS = {
    "cricket": -5,
    "movie": -5,
    "celebrity": -5,
    "football": -5,
    "election": -1,
    "crime": -4,
    "weather": -4,
}


@dataclass
class NewsItem:
    region: str
    title: str
    description: str | None
    published: str | None
    url: str | None
    publisher: str | None
    query: str
    relevance_score: int


def _normalize_space(value: str | None) -> str:
    return " ".join((value or "").split())


def _term_score(text: str, terms: dict[str, int]) -> int:
    score = 0
    for term, weight in terms.items():
        if term in text:
            score += weight
    return score


def _score_text(title: str, description: str | None) -> int:
    title_text = title.lower()
    desc_text = (description or "").lower()
    score = (_term_score(title_text, MARKET_TERMS) * 2) + _term_score(desc_text, MARKET_TERMS)
    score += _term_score(title_text, NOISE_TERMS)
    score += _term_score(desc_text, NOISE_TERMS)
    return score


def _parse_published(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return parsedate_to_datetime(value).timestamp()
    except Exception:
        return 0.0


def _publisher_name(raw: object) -> str | None:
    if isinstance(raw, dict):
        return raw.get("title") or raw.get("name")
    if isinstance(raw, str):
        return raw
    return None


def _dedupe(items: Iterable[NewsItem]) -> list[NewsItem]:
    deduped: dict[str, NewsItem] = {}
    for item in items:
        key = (item.url or item.title).strip().lower()
        existing = deduped.get(key)
        if not existing:
            deduped[key] = item
            continue
        if item.relevance_score > existing.relevance_score:
            deduped[key] = item
            continue
        if (
            item.relevance_score == existing.relevance_score
            and _parse_published(item.published) > _parse_published(existing.published)
        ):
            deduped[key] = item
    return list(deduped.values())


def _sort_items(items: Iterable[NewsItem]) -> list[NewsItem]:
    return sorted(
        items,
        key=lambda item: (item.relevance_score, _parse_published(item.published)),
        reverse=True,
    )


def _fetch_region_news(region: str, country: str, queries: list[str], max_results: int) -> list[NewsItem]:
    google_news = GNews(
        language="en",
        country=country,
        period="1d",
        max_results=max_results,
    )
    collected: list[NewsItem] = []

    for query in queries:
        try:
            raw_items = google_news.get_news(query) or []
        except Exception as exc:
            print(f"[warn] query failed for {region} '{query}': {exc}", file=sys.stderr)
            continue

        for raw in raw_items:
            title = _normalize_space(raw.get("title"))
            description = _normalize_space(raw.get("description")) or None
            score = _score_text(title, description)
            if score < 2:
                continue

            collected.append(
                NewsItem(
                    region=region,
                    title=title,
                    description=description,
                    published=raw.get("published date"),
                    url=raw.get("url"),
                    publisher=_publisher_name(raw.get("publisher")),
                    query=query,
                    relevance_score=score,
                )
            )

    return _sort_items(_dedupe(collected))


def build_ticker(domestic: list[NewsItem], global_news: list[NewsItem], max_items: int = 10) -> str:
    combined = _sort_items([*domestic, *global_news])[:max_items]
    parts = []
    for item in combined:
        prefix = "INDIA" if item.region == "domestic" else "GLOBAL"
        publisher = f" ({item.publisher})" if item.publisher else ""
        parts.append(f"[{prefix}] {item.title}{publisher}")
    return "  |  ".join(parts)


def run(domestic_limit: int, global_limit: int) -> dict:
    if GNews is None:
        raise RuntimeError(
            "Missing dependency 'gnews'. Install it with: pip install gnews"
        )

    started = time.perf_counter()

    domestic_started = time.perf_counter()
    domestic = _fetch_region_news(
        region="domestic",
        country="IN",
        queries=DOMESTIC_QUERIES,
        max_results=max(domestic_limit * 3, 12),
    )[:domestic_limit]
    domestic_time_ms = round((time.perf_counter() - domestic_started) * 1000, 1)

    global_started = time.perf_counter()
    global_news = _fetch_region_news(
        region="global",
        country="US",
        queries=GLOBAL_QUERIES,
        max_results=max(global_limit * 3, 12),
    )[:global_limit]
    global_time_ms = round((time.perf_counter() - global_started) * 1000, 1)

    total_time_ms = round((time.perf_counter() - started) * 1000, 1)

    return {
        "summary": {
            "domestic_count": len(domestic),
            "global_count": len(global_news),
            "total_count": len(domestic) + len(global_news),
            "timings_ms": {
                "domestic": domestic_time_ms,
                "global": global_time_ms,
                "total": total_time_ms,
            },
        },
        "domestic": [asdict(item) for item in domestic],
        "global": [asdict(item) for item in global_news],
        "ticker": build_ticker(domestic, global_news),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch filtered stock-market news using gnews.")
    parser.add_argument("--domestic-limit", type=int, default=6, help="Domestic headlines to keep.")
    parser.add_argument("--global-limit", type=int, default=6, help="Global headlines to keep.")
    parser.add_argument("--json-only", action="store_true", help="Print only JSON output.")
    args = parser.parse_args()

    try:
        payload = run(args.domestic_limit, args.global_limit)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 1

    if not args.json_only:
        print("Market News Fetch")
        print(json.dumps(payload["summary"], indent=2))
        print("\nTicker Preview")
        print(payload["ticker"] or "(no relevant headlines found)")
        print("\nJSON Output")

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
