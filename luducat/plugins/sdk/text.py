# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# text.py

"""Text normalisation utilities for plugin use.

Self-contained: zero imports from ``luducat.core``.  Duplicated from
``core/database.py`` helper functions so third-party plugins never touch
GPL code.

Usage in plugins::

    from luducat.plugins.sdk.text import normalize_title
"""

from __future__ import annotations

import re


# ── Internal helpers ─────────────────────────────────────────────────


def _strip_edition_suffixes(title: str) -> str:
    """Strip trailing edition/remaster suffixes that cause cross-store mismatches.

    Conservative: only strips suffixes known to differ between stores.
    Trailing-only ($ anchor) to avoid mangling mid-title words.
    """
    _EDITION_WORDS = (
        r"definitive|enhanced|remaster(?:ed)?|goty|game of the year|gold|platinum|"
        r"deluxe|ultimate|complete|special|classic|hd|standard|"
        r"director'?s?\s*cut|redux|deathinitive|royal"
    )

    # "Game - Definitive Edition" or "Game: Enhanced Edition"
    # separator + suffix word + optional "Edition"
    title = re.sub(
        rf"\s*[-:]\s*(?:{_EDITION_WORDS})(?:\s+edition)?\s*$", "", title, flags=re.IGNORECASE
    )

    # "Game GOTY Edition" or "Game Gold Edition" (suffix word + "Edition")
    title = re.sub(
        rf"\s+(?:{_EDITION_WORDS})\s+edition\s*$", "", title, flags=re.IGNORECASE
    )

    # "Game (Special Edition)" — parenthesized edition
    title = re.sub(
        rf"\s*\(\s*(?:{_EDITION_WORDS})(?:\s+edition)?\s*\)\s*$", "", title, flags=re.IGNORECASE
    )

    # Bare trailing qualifier: "Game HD", "Game GOTY", "Game Gold", etc.
    _BARE_SUFFIXES = (
        r"hd|classic|goty|gold|platinum|redux|remastered|remaster|deluxe"
    )
    title = re.sub(rf"\s+(?:{_BARE_SUFFIXES})\s*$", "", title, flags=re.IGNORECASE)

    return title


def _roman_to_arabic(title: str) -> str:
    """Convert standalone Roman numerals (II-XX) to Arabic in a title.

    Word-boundary-safe. Single 'I' excluded (too many false positives:
    'I Am Alive', 'I Expect You To Die'). V and X included (rare as
    standalone non-numeral words in game titles).
    """
    _ROMAN_MAP = {
        "XX": "20", "XIX": "19", "XVIII": "18", "XVII": "17", "XVI": "16",
        "XV": "15", "XIV": "14", "XIII": "13", "XII": "12", "XI": "11",
        "X": "10", "IX": "9", "VIII": "8", "VII": "7", "VI": "6",
        "V": "5", "IV": "4", "III": "3", "II": "2",
    }

    # Build pattern: longest first to avoid partial matches (XVIII before XVI etc.)
    _ROMAN_PATTERN = re.compile(
        r"\b(" + "|".join(_ROMAN_MAP.keys()) + r")\b", re.IGNORECASE
    )

    def _replace(m):
        return _ROMAN_MAP[m.group(1).upper()]

    return _ROMAN_PATTERN.sub(_replace, title)


# ── Public API ───────────────────────────────────────────────────────


def normalize_title(title: str) -> str:
    """Normalize game title for cross-store deduplication.

    Pipeline (order matters):
    1. & -> and (before punctuation strip)
    2. Strip TM/R/C and (tm)/(r)/(c) text markers
    3. Remove parenthesized years -- (2012), (1998)
    4. Strip edition suffixes (trailing only, conservative)
    5. Strip leading articles (the, a, an)
    6. Remove mid-title "the" after : or -
    7. Roman -> Arabic numerals (II-XX, word-boundary-safe)
    8. Remove punctuation + collapse whitespace
    """
    # Lowercase
    normalized = title.lower()

    # 1. & -> and
    normalized = normalized.replace("&", "and")

    # 2. Strip trademark symbols and text markers
    normalized = re.sub(r"[\u2122\u00ae\u00a9]", "", normalized)
    normalized = re.sub(r"\((?:tm|r|c)\)", "", normalized, flags=re.IGNORECASE)

    # 3. Remove parenthesized years -- (2012), (1998)
    normalized = re.sub(r"\s*\(\d{4}\)\s*", " ", normalized)

    # 4. Strip edition suffixes
    normalized = _strip_edition_suffixes(normalized)

    # 5. Strip leading articles
    articles = ["the ", "a ", "an "]
    for article in articles:
        if normalized.startswith(article):
            normalized = normalized[len(article):]
            break

    # 6. Remove mid-title "the" after : or -
    normalized = re.sub(r"([:–—-])\s*the\s+", r"\1 ", normalized)

    # 7. Roman -> Arabic numerals
    normalized = _roman_to_arabic(normalized)

    # 8. Remove punctuation and extra whitespace
    normalized = re.sub(r"[^\w\s]", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    return normalized
