# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# game_detector.py

"""Multi-signal DOSBox game detection.

Evaluates multiple data sources to determine if a game is a DOS game
that should be launched with DOSBox. Returns a PlatformCandidate with
confidence score, or None if no signals match.
"""

import logging
from typing import Optional

from luducat.plugins.platforms.shared.detection import PlatformCandidate
from luducat.plugins.platforms.shared.platform_query import PlatformDataQuery

logger = logging.getLogger(__name__)

# IGDB platform ID for DOS
_IGDB_DOS_PLATFORM = 13


def detect_dosbox_game(
    game,
    platform_query: Optional[PlatformDataQuery] = None,
) -> Optional[PlatformCandidate]:
    """Detect if a game should run under DOSBox.

    Signal chain (first match >= 90 short-circuits):

    1. User saved platform = dosbox          → 100
    2. GOG is_using_dosbox flag              → 95
    3. IGDB platform_id = 13 (DOS)           → 90
    4. Metadata has dosbox_conf key          → 88
    5. PCGamingWiki engine = "DOSBox"        → 88
    6. Tag contains "dos"/"dosbox"           → 85
    7. Genre contains "dos"                  → 80
    8. GOG + pre-1996 + genre match          → 50
    9. Known DOS title substring             → 40

    Args:
        game: GameEntry or compatible object with .get() / getattr access
        platform_query: Optional cross-plugin DB query object

    Returns:
        PlatformCandidate with highest-scoring signal, or None
    """
    game_id = _get_attr(game, "id", "unknown")
    best: Optional[PlatformCandidate] = None

    def _consider(score: int, source: str, reason: str,
                  hint: dict = None) -> bool:
        nonlocal best
        if best is None or score > best.score:
            best = PlatformCandidate(
                game_id=game_id, platform="dosbox",
                score=score, source=source, reason=reason,
                detection_hint=hint,
            )
        return score >= PlatformCandidate.AUTO_ASSIGN

    # 1. User saved platform override
    launch_config = _get_attr(game, "launch_config", "")
    if launch_config:
        import json
        try:
            config = json.loads(launch_config) if isinstance(launch_config, str) else launch_config
            if isinstance(config, dict) and config.get("platform") == "dosbox":
                if _consider(100, "user_saved", "User-selected DOSBox platform"):
                    return best
        except (json.JSONDecodeError, TypeError):
            pass

    # 2. GOG is_using_dosbox flag
    if platform_query:
        store_app_ids = _get_attr(game, "store_app_ids", {}) or {}
        gog_id = store_app_ids.get("gog")
        if gog_id:
            dosbox_flag = platform_query.is_gog_dosbox_game(str(gog_id))
            if dosbox_flag is True:
                if _consider(95, "gog_dosbox_flag",
                             "GOG is_using_dosbox=True"):
                    return best

    # 3. IGDB platform_id = 13 (DOS)
    if platform_query:
        store_app_ids = _get_attr(game, "store_app_ids", {}) or {}
        for store_name, store_app_id in store_app_ids.items():
            platform_ids = platform_query.get_igdb_platform_ids(
                store_name, str(store_app_id)
            )
            if _IGDB_DOS_PLATFORM in platform_ids:
                if _consider(90, "igdb_platform",
                             "IGDB platform: DOS"):
                    return best
                break

    # 4. Metadata has dosbox_conf key
    extra_metadata = _get_attr(game, "extra_metadata", {}) or {}
    if "dosbox_conf" in extra_metadata or "dosbox" in extra_metadata:
        if _consider(88, "metadata", "Game metadata contains DOSBox config"):
            return best

    # 5. PCGamingWiki engine = "DOSBox"
    if platform_query:
        store_app_ids = _get_attr(game, "store_app_ids", {}) or {}
        for store_name, store_app_id in store_app_ids.items():
            engines = platform_query.get_pcgw_engines(
                store_name, str(store_app_id)
            )
            for engine in engines:
                if "dosbox" in engine.lower():
                    if _consider(88, "pcgw_engine",
                                 f"PCGamingWiki engine: {engine}"):
                        return best
                    break

    # 6. Tag contains "dos"/"dosbox"
    tags = _get_attr(game, "tags", []) or []
    if isinstance(tags, str):
        tags = [tags]
    for tag in tags:
        tag_lower = tag.lower() if isinstance(tag, str) else ""
        if "dosbox" in tag_lower or tag_lower == "dos":
            if _consider(85, "tag_match", f"Tag: {tag}"):
                return best
            break

    # 7. Genre contains "dos"
    genres = _get_attr(game, "genres", []) or []
    for genre in genres:
        if isinstance(genre, str) and "dos" in genre.lower():
            if _consider(80, "tag_match", f"Genre: {genre}"):
                return best
            break

    # 8. GOG + pre-1996 + genre match
    stores = _get_attr(game, "stores", []) or []
    if "gog" in stores:
        release_date = _get_attr(game, "release_date", "")
        if isinstance(release_date, str) and release_date:
            year = _extract_year(release_date)
            if year and year < 1996:
                # Check for game genres typical of DOS era
                genre_set = {
                    g.lower() for g in genres if isinstance(g, str)
                }
                dos_genres = {
                    "action", "adventure", "rpg", "strategy",
                    "puzzle", "simulation", "shooter",
                }
                if genre_set & dos_genres:
                    _consider(
                        50, "title_heuristic",
                        f"GOG game from {year} with matching genre",
                    )

    # 9. Known DOS title substrings
    title = _get_attr(game, "title", "")
    if isinstance(title, str) and title:
        title_lower = title.lower()
        for keyword in _DOS_TITLE_HINTS:
            if keyword in title_lower:
                _consider(40, "title_heuristic",
                          f"Title contains '{keyword}'")
                break

    if best and best.score >= PlatformCandidate.SUGGEST:
        return best
    return None


# Known DOS-era title fragments
_DOS_TITLE_HINTS = [
    "commander keen",
    "wolfenstein 3d",
    "doom",
    "duke nukem",
    "jazz jackrabbit",
    "x-com",
    "xcom",
    "ultima",
    "might and magic",
    "eye of the beholder",
    "wasteland",
    "starflight",
    "wing commander",
    "privateer",
    "crusader: no",
    "system shock",
    "lands of lore",
    "alone in the dark",
    "master of orion",
    "master of magic",
    "colonization",
    "sid meier",
    "simcity",
    "theme hospital",
    "theme park",
    "transport tycoon",
    "worms",
    "lemmings",
    "prince of persia",
    "another world",
    "flashback",
    "dune",
    "settlers",
    "heroes of might",
]


def _get_attr(obj, name: str, default=None):
    """Get attribute from game object, supporting both attr and dict access."""
    try:
        val = getattr(obj, name, None)
        if val is not None:
            return val
    except Exception:
        pass
    if hasattr(obj, "get"):
        return obj.get(name, default)
    return default


def _extract_year(date_str: str) -> Optional[int]:
    """Extract year from a date string."""
    import re
    match = re.search(r"(\d{4})", date_str)
    if match:
        year = int(match.group(1))
        if 1970 <= year <= 2100:
            return year
    return None
