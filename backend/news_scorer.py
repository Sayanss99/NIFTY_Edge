"""Rule-based news scorer for NiftyEdge News Desk.

Single public entry point:
    score_news_item(headline, summary, source_weight) -> NewsScore

Pure function — no DB access, no I/O.
Tune thresholds in config.py (NEWS_TIER_*) and keywords in news_entities.py.
"""

import re
from dataclasses import dataclass
from typing import Optional

from news_entities import (
    CATEGORIES,
    BULLISH_WORDS,
    BEARISH_WORDS,
    EXCLUDE_KEYWORDS,
    INVERSE_CONTEXT,
    NEGATORS,
    MIN_STORE_SCORE,
)
from config import NEWS_TIER_CRITICAL, NEWS_TIER_HIGH, NEWS_TIER_MEDIUM, NEWS_TIER_LOW

SCORER_VERSION = "rules-v2"

# Negator lookback window — tokens preceding a direction word that can negate it
_NEGATOR_WINDOW = 2


@dataclass
class NewsScore:
    raw_impact_score: float
    impact_tier: str           # CRITICAL | HIGH | MEDIUM | LOW | IRRELEVANT
    direction: str             # BULLISH | BEARISH | NEUTRAL
    category: str              # top matched category name
    matched_keywords: dict     # {category_name: [kw1, kw2, ...]}
    direction_word_counts: dict  # {"bullish": int, "bearish": int}
    scorer_version: str = SCORER_VERSION
    should_store: bool = True  # False if score < MIN_STORE_SCORE or excluded


def _tokenize(text: str) -> list[str]:
    """Simple alphanumeric tokenizer (keeps words like 'f&o' as 'f' 'o' — acceptable)."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _is_negated(tokens: list[str], idx: int) -> bool:
    """True if any negator appears in the _NEGATOR_WINDOW tokens before idx."""
    start = max(0, idx - _NEGATOR_WINDOW)
    window = tokens[start:idx]
    for neg in NEGATORS:
        if " " in neg:
            # multi-word negator — check joined window
            if neg in " ".join(window):
                return True
        elif neg in window:
            return True
    return False


def _count_directional_words(text: str, tokens: list[str]) -> tuple[int, int]:
    """Count bullish / bearish words, respecting negation.
    Single-word matches use token-level negation check;
    multi-word phrases fall back to substring match (no negation).
    """
    bullish = 0
    bearish = 0

    # Index tokens for quick lookup
    token_positions: dict[str, list[int]] = {}
    for i, t in enumerate(tokens):
        token_positions.setdefault(t, []).append(i)

    for w in BULLISH_WORDS:
        if " " in w or "-" in w:
            if w in text:
                bullish += 1
        else:
            for idx in token_positions.get(w, []):
                if not _is_negated(tokens, idx):
                    bullish += 1

    for w in BEARISH_WORDS:
        if " " in w or "-" in w:
            if w in text:
                bearish += 1
        else:
            for idx in token_positions.get(w, []):
                if not _is_negated(tokens, idx):
                    bearish += 1

    return bullish, bearish


def _apply_inverse_context(text: str, bullish: int, bearish: int) -> tuple[int, int, int]:
    """Apply context-aware direction flips. Returns (new_bullish, new_bearish, flips)."""
    flips = 0
    for ctx_word, dir_word, new_direction in INVERSE_CONTEXT:
        if ctx_word in text and dir_word in text:
            # Both appear — flip one vote from the default sense to the new one.
            if dir_word in BULLISH_WORDS and new_direction == "bearish":
                if bullish > 0:
                    bullish -= 1
                bearish += 1
                flips += 1
            elif dir_word in BEARISH_WORDS and new_direction == "bullish":
                if bearish > 0:
                    bearish -= 1
                bullish += 1
                flips += 1
            elif new_direction == "bullish":
                bullish += 1
                flips += 1
            elif new_direction == "bearish":
                bearish += 1
                flips += 1
    return bullish, bearish, flips


def score_news_item(
    headline: str,
    summary: Optional[str],
    source_weight: float = 1.0,
) -> NewsScore:
    """Score a news item on impact tier, directional bias, and category.

    Pipeline:
      1. Exclude off-topic (sports / entertainment / lifestyle) → IRRELEVANT
      2. Match categories; require ≥1 match or skip
      3. Count directional words with negation handling
      4. Apply context-aware direction inversion (VIX surge = bearish, etc.)
      5. Compute final score, tier.
    """
    text = f"{headline} {summary or ''}".lower()
    tokens = _tokenize(text)

    # ── Step 1: off-topic exclusion ────────────────────────────────────────────
    for bad in EXCLUDE_KEYWORDS:
        if bad in text:
            return NewsScore(
                raw_impact_score=0.0,
                impact_tier="IRRELEVANT",
                direction="NEUTRAL",
                category="EXCLUDED",
                matched_keywords={},
                direction_word_counts={"bullish": 0, "bearish": 0},
                should_store=False,
            )

    # ── Step 2: Category matching ──────────────────────────────────────────────
    matched_keywords: dict[str, list[str]] = {}
    category_scores: dict[str, float] = {}

    for cat_name, cat_cfg in CATEGORIES.items():
        hits = [kw for kw in cat_cfg["keywords"] if kw in text]
        if hits:
            matched_keywords[cat_name] = hits
            category_scores[cat_name] = len(hits) * cat_cfg["weight"]

    if not category_scores:
        # No market-relevant category matched — drop as irrelevant.
        return NewsScore(
            raw_impact_score=0.0,
            impact_tier="IRRELEVANT",
            direction="NEUTRAL",
            category="GENERAL",
            matched_keywords={},
            direction_word_counts={"bullish": 0, "bearish": 0},
            should_store=False,
        )

    top_category = max(category_scores, key=category_scores.get)
    base_score = sum(category_scores.values())

    # ── Step 3: Direction scoring (with negation) ──────────────────────────────
    bullish_count, bearish_count = _count_directional_words(text, tokens)

    # ── Step 4: Context-aware inversion ────────────────────────────────────────
    bullish_count, bearish_count, _ = _apply_inverse_context(
        text, bullish_count, bearish_count
    )

    if bullish_count > bearish_count:
        direction = "BULLISH"
    elif bearish_count > bullish_count:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    # ── Step 5: Final score & tier ─────────────────────────────────────────────
    certainty_bonus = abs(bullish_count - bearish_count) * 2.0
    raw_score = (base_score + certainty_bonus) * source_weight

    if raw_score >= NEWS_TIER_CRITICAL:
        tier = "CRITICAL"
    elif raw_score >= NEWS_TIER_HIGH:
        tier = "HIGH"
    elif raw_score >= NEWS_TIER_MEDIUM:
        tier = "MEDIUM"
    elif raw_score >= NEWS_TIER_LOW:
        tier = "LOW"
    else:
        tier = "IRRELEVANT"

    return NewsScore(
        raw_impact_score=round(raw_score, 2),
        impact_tier=tier,
        direction=direction,
        category=top_category,
        matched_keywords=matched_keywords,
        direction_word_counts={"bullish": bullish_count, "bearish": bearish_count},
        should_store=raw_score >= MIN_STORE_SCORE,
    )
