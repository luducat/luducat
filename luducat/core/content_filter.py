# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# content_filter.py

"""Content filtering via multi-source confidence scoring.

Every store and metadata plugin can contribute evidence that a game
contains adult content.  Signals are **additive** (not priority-based)
— the more independent sources agree, the higher the confidence.

The ``adult_confidence()`` function returns a 0.0–1.0 score from a
single (priority-resolved) metadata dict.

The ``adult_confidence_from_sources()`` function aggregates signals
across ALL available sources (bypassing priority resolution) for
maximum coverage.  This is the preferred entry point at cache-build
time.

A configurable **threshold** (default 0.60) decides whether the game
is hidden.  This lets users tune the filter: strict (0.3), moderate
(0.6), or permissive (0.9).

Signal weights are designed so that a single strong signal (Steam
"Adult Only Sexual Content") is enough to cross the default threshold
on its own, while weaker signals (PEGI 18, which also covers violence)
need corroboration.

Steam content descriptor IDs (from GetAppDetails API):
    1 = Some Nudity or Sexual Content
    2 = Frequent Violence or Gore
    3 = Adult Only Sexual Content
    4 = Frequent Nudity or Sexual Content
    5 = General Mature Content

IGDB age rating mappings (from provider.py):
    ESRB category=1: rating 12 = "AO" (Adults Only)
    PEGI  category=2: rating  5 = "18"
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Default threshold ─────────────────────────────────────────────────

DEFAULT_ADULT_THRESHOLD: float = 0.60

# ── Signal weights ────────────────────────────────────────────────────
# Each weight represents how strongly a single signal suggests adult
# content.  Weights are capped to [0, 1] after aggregation.

# Steam content descriptor weights (by descriptor ID)
_STEAM_DESCRIPTOR_WEIGHTS: Dict[int, float] = {
    3: 0.95,  # Adult Only Sexual Content  — near-certain
    4: 0.75,  # Frequent Nudity or Sexual Content
    1: 0.15,  # Some Nudity or Sexual Content — very common in mainstream
    # 2 (violence) and 5 (general mature) intentionally omitted
}

# IGDB age rating weights (by system + rating string)
# Also covers GOG content_ratings (converted to same format in _standardize_metadata)
_IGDB_RATING_WEIGHTS: Dict[str, float] = {
    "ESRB:AO":  0.90,   # Adults Only — strong signal
    "PEGI:18":  0.30,    # PEGI 18 covers violence too, needs corroboration
    "ESRB:M":   0.10,    # Mature — very common, weak signal alone
    "ACB:R18+": 0.85,    # Australian R18+ — strong
    "USK:18":   0.30,    # German 18 — similar scope to PEGI 18
    # GOG content ratings (numeric ageRating values from catalog API)
    "GOG:18":   0.30,    # GOG's own 18+ rating — same weight as PEGI 18
    "BR:18":    0.30,    # Brazil 18 rating
    "GRAC:18":  0.30,    # Korea 18 rating
}

# required_age field weight (scaled by age value)
_REQUIRED_AGE_18_WEIGHT: float = 0.35  # required_age >= 18

# ── File-based keyword scoring ───────────────────────────────────────

_keyword_rules: Optional[Dict[tuple, float]] = None
_KEYWORDS_FILE = Path(__file__).parent.parent / "assets" / "contentfilter" / "keywords.txt"


def _load_keyword_rules() -> Dict[tuple, float]:
    """Read keyword rules file, return {(field, keyword): score} dict."""
    rules: Dict[tuple, float] = {}
    try:
        text = _KEYWORDS_FILE.read_text(encoding="utf-8")
    except (OSError, IOError):
        return rules
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Parse score from the end first (colon-safe)
        parts = line.rsplit(":", 1)
        if len(parts) != 2:
            logger.debug("keyword rules: malformed line: %s", line)
            continue
        rest, score_str = parts
        # Parse field:keyword
        fk = rest.split(":", 1)
        if len(fk) != 2:
            logger.debug("keyword rules: malformed line: %s", line)
            continue
        field, keyword = fk[0].strip().lower(), fk[1].strip().lower()
        if not field or not keyword:
            logger.debug("keyword rules: empty field or keyword: %s", line)
            continue
        try:
            score = float(score_str.strip())
        except ValueError:
            logger.debug("keyword rules: bad score: %s", line)
            continue
        rules[(field, keyword)] = score
    return rules


def _get_keyword_rules() -> Dict[tuple, float]:
    """Lazy-cached access to keyword rules."""
    global _keyword_rules
    if _keyword_rules is None:
        _keyword_rules = _load_keyword_rules()
    return _keyword_rules


def _compute_keyword_score(metadata: dict) -> float:
    """Score a single metadata dict against keyword rules."""
    rules = _get_keyword_rules()
    if not rules:
        return 0.0
    total = 0.0
    lowered: Dict[str, Any] = {}
    for (field, keyword), score in rules.items():
        value = metadata.get(field)
        if value is None:
            continue
        if field not in lowered:
            if isinstance(value, list):
                lowered[field] = [str(v).lower() for v in value]
            elif isinstance(value, str):
                lowered[field] = value.lower()
            else:
                continue
        cached = lowered[field]
        if isinstance(cached, list):
            if keyword in cached:
                total += score
        elif field == "title":
            if re.search(r"\b" + re.escape(keyword) + r"\b", cached):
                total += score
        elif cached == keyword:
            total += score
    return total


def _compute_keyword_score_from_sources(metadata_by_store: dict) -> float:
    """Score keyword rules against the union of all sources."""
    rules = _get_keyword_rules()
    if not rules:
        return 0.0
    # Build union of field values across all stores
    field_values: Dict[str, Any] = {}
    for metadata in metadata_by_store.values():
        if not isinstance(metadata, dict):
            continue
        for field in {f for f, _k in rules}:
            value = metadata.get(field)
            if value is None:
                continue
            if isinstance(value, list):
                if field not in field_values:
                    field_values[field] = set()
                for v in value:
                    field_values[field].add(str(v).lower())
            elif isinstance(value, str):
                if field not in field_values:
                    field_values[field] = set()
                field_values[field].add(value.lower())
    if not field_values:
        return 0.0
    total = 0.0
    for (field, keyword), score in rules.items():
        vals = field_values.get(field)
        if not vals:
            continue
        if field == "title":
            for v in vals:
                if re.search(r"\b" + re.escape(keyword) + r"\b", v):
                    total += score
                    break
        elif keyword in vals:
            total += score
    return total


def _reset_keyword_cache():
    """Reset the keyword rules cache (for testing)."""
    global _keyword_rules
    _keyword_rules = None


def adult_confidence(metadata: dict) -> float:
    """Compute adult-content confidence score from all available signals.

    Signals are **additive but diminishing** — each new signal adds
    progressively less if confidence is already high.  This uses the
    formula ``1 - product(1 - w_i)`` which naturally caps at 1.0 and
    gives strongest-signal-wins behavior.

    Args:
        metadata: Merged metadata dict (from games cache or resolver).
                  May contain fields from multiple store/metadata plugins.

    Returns:
        Confidence score in [0.0, 1.0].  Higher = more likely adult.
    """
    weights: List[float] = []

    # ── Store-level adult baseline (declarative engine rulesets) ──
    store_baseline = metadata.get("store_adult_baseline")
    if isinstance(store_baseline, (int, float)) and store_baseline > 0:
        weights.append(min(float(store_baseline), 1.0))

    # ── Steam content descriptors ─────────────────────────────────
    _collect_steam_descriptor_weights(metadata, weights)

    # ── required_age ──────────────────────────────────────────────
    required_age = metadata.get("required_age")
    if isinstance(required_age, (int, float)) and required_age >= 18:
        weights.append(_REQUIRED_AGE_18_WEIGHT)

    # ── IGDB / other age ratings ──────────────────────────────────
    _collect_age_rating_weights(metadata, weights)

    # ── Keyword scoring ──────────────────────────────────────────
    keyword_score = _compute_keyword_score(metadata)
    if keyword_score > 0:
        weights.append(min(keyword_score, 1.0))

    if not weights:
        return 0.0

    combined = _combine_weights(weights)

    if keyword_score < 0:
        combined = max(0.0, combined + keyword_score)

    return combined


def adult_confidence_from_sources(
    metadata_by_store: Dict[str, Dict],
    tags: Optional[List[Dict]] = None,
    game_nsfw_override: int = 0,
) -> float:
    """Compute adult-content confidence by aggregating ALL sources.

    Unlike ``adult_confidence()`` which scores a single priority-resolved
    dict, this function iterates every source in ``metadata_by_store``
    and collects signals from all of them.  Identical signals from
    different sources (e.g., PEGI:18 from both IGDB and GOG) are
    deduplicated — same evidence from two providers is not independent
    confirmation.

    Override priority chain (highest to lowest):
        1. Per-game nsfw_override (-1=SFW, +1=NSFW)
        2. Per-tag nsfw_override  (-1=SFW wins over +1=NSFW at same level)
        3. Metadata scoring (computed from all sources)

    Args:
        metadata_by_store: Dict mapping source name to metadata dict.
                          e.g. {"steam": {...}, "igdb": {...}, "gog": {...}}
        tags: Optional list of tag dicts, each may have "nsfw_override" key.
        game_nsfw_override: Per-game override (0=neutral, 1=NSFW, -1=SFW).

    Returns:
        Confidence score in [0.0, 1.0].
    """
    # ── Per-game override (highest priority) ──────────────────────
    if game_nsfw_override == -1:
        return 0.0
    if game_nsfw_override == 1:
        return 1.0

    # ── Per-tag overrides ─────────────────────────────────────────
    if tags:
        has_nsfw_tag = False
        for tag in tags:
            override = tag.get("nsfw_override", 0)
            if override == -1:
                return 0.0  # SFW wins — explicit user whitelist
            if override == 1:
                has_nsfw_tag = True
        if has_nsfw_tag:
            return 1.0

    # ── Aggregate signals from ALL sources ────────────────────────
    if not metadata_by_store:
        return 0.0

    # Use sets to deduplicate identical signals across stores
    seen_descriptor_ids: set = set()
    seen_age_rating_keys: set = set()
    seen_required_age_18 = False

    weights: List[float] = []

    seen_store_baseline = False

    for _store, metadata in metadata_by_store.items():
        if not isinstance(metadata, dict):
            continue

        # Store-level adult baseline (declarative engine rulesets)
        store_baseline = metadata.get("store_adult_baseline")
        if (
            isinstance(store_baseline, (int, float))
            and store_baseline > 0
            and not seen_store_baseline
        ):
            seen_store_baseline = True
            weights.append(min(float(store_baseline), 1.0))

        # Content descriptors (Steam-specific but could come via enrichment)
        _collect_descriptor_weights_dedup(metadata, weights, seen_descriptor_ids)

        # required_age
        required_age = metadata.get("required_age")
        if (
            isinstance(required_age, (int, float))
            and required_age >= 18
            and not seen_required_age_18
        ):
            seen_required_age_18 = True
            weights.append(_REQUIRED_AGE_18_WEIGHT)

        # Age ratings (IGDB, GOG, etc.)
        _collect_age_rating_weights_dedup(metadata, weights, seen_age_rating_keys)

    # ── Keyword scoring (union across all sources) ───────────────
    keyword_score = _compute_keyword_score_from_sources(metadata_by_store)
    if keyword_score > 0:
        weights.append(min(keyword_score, 1.0))

    if not weights:
        return 0.0

    combined = _combine_weights(weights)

    if keyword_score < 0:
        combined = max(0.0, combined + keyword_score)

    return combined


def is_adult_content(metadata: dict, threshold: float = DEFAULT_ADULT_THRESHOLD) -> bool:
    """Check whether a game exceeds the adult-content confidence threshold.

    Convenience wrapper around ``adult_confidence()``.

    Args:
        metadata: Merged metadata dict.
        threshold: Confidence threshold (0.0–1.0).  Default 0.60.

    Returns:
        True if the game should be hidden by the content filter.
    """
    return adult_confidence(metadata) >= threshold


# ── Internal helpers ──────────────────────────────────────────────────

def _combine_weights(weights: List[float]) -> float:
    """Combine weights via independent-probability formula: 1 - product(1 - w_i)."""
    product = 1.0
    for w in weights:
        product *= (1.0 - min(w, 1.0))
    return 1.0 - product


def _collect_steam_descriptor_weights(
    metadata: dict, weights: List[float]
) -> None:
    """Extract weights from Steam content_descriptors field."""
    descriptors = metadata.get("content_descriptors")

    ids: Any = None
    if isinstance(descriptors, list):
        ids = descriptors
    elif isinstance(descriptors, dict):
        ids = descriptors.get("ids") or []

    if not isinstance(ids, list):
        return

    for desc_id in ids:
        if isinstance(desc_id, int) and desc_id in _STEAM_DESCRIPTOR_WEIGHTS:
            weights.append(_STEAM_DESCRIPTOR_WEIGHTS[desc_id])


def _collect_descriptor_weights_dedup(
    metadata: dict, weights: List[float], seen: set
) -> None:
    """Extract weights from content_descriptors, deduplicating across sources."""
    descriptors = metadata.get("content_descriptors")

    ids: Any = None
    if isinstance(descriptors, list):
        ids = descriptors
    elif isinstance(descriptors, dict):
        ids = descriptors.get("ids") or []

    if not isinstance(ids, list):
        return

    for desc_id in ids:
        if isinstance(desc_id, int) and desc_id in _STEAM_DESCRIPTOR_WEIGHTS:
            if desc_id not in seen:
                seen.add(desc_id)
                weights.append(_STEAM_DESCRIPTOR_WEIGHTS[desc_id])


def _collect_age_rating_weights(
    metadata: dict, weights: List[float]
) -> None:
    """Extract weights from IGDB-style age_ratings list."""
    age_ratings = metadata.get("age_ratings")
    if not isinstance(age_ratings, list):
        return

    for rating in age_ratings:
        if not isinstance(rating, dict):
            continue
        system = rating.get("system", "")
        value = rating.get("rating", "")
        key = f"{system}:{value}"
        if key in _IGDB_RATING_WEIGHTS:
            weights.append(_IGDB_RATING_WEIGHTS[key])


def _collect_age_rating_weights_dedup(
    metadata: dict, weights: List[float], seen: set
) -> None:
    """Extract weights from age_ratings, deduplicating across sources."""
    age_ratings = metadata.get("age_ratings")
    if not isinstance(age_ratings, list):
        return

    for rating in age_ratings:
        if not isinstance(rating, dict):
            continue
        system = rating.get("system", "")
        value = rating.get("rating", "")
        key = f"{system}:{value}"
        if key in _IGDB_RATING_WEIGHTS and key not in seen:
            seen.add(key)
            weights.append(_IGDB_RATING_WEIGHTS[key])
