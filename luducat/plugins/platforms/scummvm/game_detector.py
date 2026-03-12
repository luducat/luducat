# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# game_detector.py

"""Multi-signal ScummVM game detection.

Evaluates multiple data sources to determine if a game should be launched
with ScummVM. Returns a PlatformCandidate with confidence score, or None.

The SCUMMVM_GAME_IDS seed provides high-confidence detection for ~100
popular titles, mapping normalized titles and store IDs to ScummVM's
internal game identifiers.
"""

import logging
from typing import Dict, Optional

from luducat.plugins.platforms.shared.detection import PlatformCandidate
from luducat.plugins.platforms.shared.platform_query import PlatformDataQuery

logger = logging.getLogger(__name__)

# ScummVM game ID seed — maps titles/store IDs to ScummVM game identifiers.
# Keys are normalized title substrings, values contain the ScummVM game ID
# and optional store-specific app IDs for high-confidence matching.
SCUMMVM_GAME_IDS: Dict[str, Dict] = {
    # LucasArts / LucasFilm
    "secret of monkey island": {"id": "monkey", "gog": "1207658753"},
    "monkey island 2": {"id": "monkey2", "gog": "1207658986"},
    "curse of monkey island": {"id": "comi"},
    "escape from monkey island": {"id": "monkey4"},
    "day of the tentacle": {"id": "tentacle", "gog": "1207659062"},
    "maniac mansion": {"id": "maniac"},
    "sam & max hit the road": {"id": "samnmax", "gog": "1207658852"},
    "sam and max hit the road": {"id": "samnmax"},
    "full throttle": {"id": "ft"},
    "grim fandango": {"id": "grim"},
    "the dig": {"id": "dig"},
    "indiana jones and the fate of atlantis": {"id": "atlantis", "gog": "1207658915"},
    "indiana jones and the last crusade": {"id": "indy3"},
    "loom": {"id": "loom"},
    "zak mckracken": {"id": "zak"},
    # Sierra — King's Quest
    "king's quest i": {"id": "kq1"},
    "king's quest ii": {"id": "kq2"},
    "king's quest iii": {"id": "kq3"},
    "king's quest iv": {"id": "kq4"},
    "king's quest v": {"id": "kq5"},
    "king's quest vi": {"id": "kq6"},
    "king's quest vii": {"id": "kq7"},
    # Sierra — Space Quest
    "space quest i": {"id": "sq1"},
    "space quest ii": {"id": "sq2"},
    "space quest iii": {"id": "sq3"},
    "space quest iv": {"id": "sq4"},
    "space quest v": {"id": "sq5"},
    "space quest 6": {"id": "sq6"},
    # Sierra — Police Quest
    "police quest i": {"id": "pq1"},
    "police quest ii": {"id": "pq2"},
    "police quest iii": {"id": "pq3"},
    "police quest: open season": {"id": "pq4"},
    # Sierra — Leisure Suit Larry
    "leisure suit larry 1": {"id": "lsl1"},
    "leisure suit larry 2": {"id": "lsl2"},
    "leisure suit larry 3": {"id": "lsl3"},
    "leisure suit larry 5": {"id": "lsl5"},
    "leisure suit larry 6": {"id": "lsl6"},
    "leisure suit larry 7": {"id": "lsl7"},
    # Sierra — Quest for Glory
    "quest for glory i": {"id": "qfg1"},
    "quest for glory ii": {"id": "qfg2"},
    "quest for glory iii": {"id": "qfg3"},
    "quest for glory iv": {"id": "qfg4"},
    # Sierra — Gabriel Knight
    "gabriel knight": {"id": "gk1"},
    "gabriel knight 2": {"id": "gk2"},
    "phantasmagoria": {"id": "phantasmagoria"},
    "phantasmagoria 2": {"id": "phantasmagoria2"},
    # Sierra — Other
    "laura bow": {"id": "laurabow"},
    "the dagger of amon ra": {"id": "laurabow2"},
    "freddy pharkas": {"id": "freddypharkas"},
    "pepper's adventures in time": {"id": "pepper"},
    "torin's passage": {"id": "torin"},
    "shivers": {"id": "shivers"},
    # Revolution Software
    "broken sword": {"id": "sword1", "gog": "1207658683"},
    "broken sword ii": {"id": "sword2"},
    "broken sword 2": {"id": "sword2"},
    "beneath a steel sky": {"id": "sky", "gog": "1207658695"},
    "lure of the temptress": {"id": "lure"},
    # Westwood
    "legend of kyrandia": {"id": "kyra1"},
    "kyrandia": {"id": "kyra1"},
    "hand of fate": {"id": "kyra2"},
    "malcolm's revenge": {"id": "kyra3"},
    # Humongous Entertainment
    "putt-putt": {"id": "puttputt"},
    "freddi fish": {"id": "freddi"},
    "pajama sam": {"id": "pajama"},
    "spy fox": {"id": "spyfox"},
    # Other publishers
    "flight of the amazon queen": {"id": "queen"},
    "simon the sorcerer": {"id": "simon1"},
    "simon the sorcerer 2": {"id": "simon2"},
    "discworld": {"id": "dw"},
    "discworld 2": {"id": "dw2"},
    "dreamweb": {"id": "dreamweb"},
    "touche": {"id": "touche"},
    "cruise for a corpse": {"id": "cruise"},
    "future wars": {"id": "fw"},
    "i have no mouth": {"id": "ihnm"},
    "myst": {"id": "myst"},
    "riven": {"id": "riven"},
    "blade runner": {"id": "bladerunner"},
    "the longest journey": {"id": "tlj"},
    "syberia": {"id": "syberia"},
    "syberia ii": {"id": "syberia2"},
    "syberia 2": {"id": "syberia2"},
    "gobliiins": {"id": "gob1"},
    "gobliins 2": {"id": "gob2"},
    "goblins quest 3": {"id": "gob3"},
    "drascula": {"id": "drascula"},
    "hopkins fbi": {"id": "hopkins"},
    "nippon safes": {"id": "nippon"},
    "ringworld": {"id": "ringworld"},
    "return to ringworld": {"id": "ringworld2"},
    "star trek 25th": {"id": "startrek25"},
    "star trek judgment rites": {"id": "startrekjr"},
    "inherit the earth": {"id": "ite"},
    "teenagent": {"id": "teenagent"},
    "the neverhood": {"id": "neverhood"},
    "tony tough": {"id": "tony"},
    "composer": {"id": "composer"},
}

# Known ScummVM-compatible game series (for lower-confidence matching)
SCUMMVM_SERIES = [
    "monkey island",
    "day of the tentacle",
    "maniac mansion",
    "sam & max",
    "sam and max",
    "full throttle",
    "grim fandango",
    "the dig",
    "indiana jones",
    "loom",
    "king's quest",
    "kings quest",
    "space quest",
    "police quest",
    "leisure suit larry",
    "quest for glory",
    "gabriel knight",
    "phantasmagoria",
    "broken sword",
    "beneath a steel sky",
    "flight of the amazon queen",
    "simon the sorcerer",
    "discworld",
    "kyrandia",
    "legend of kyrandia",
    "lure of the temptress",
    "dreamweb",
    "touche",
    "cruise for a corpse",
    "future wars",
    "i have no mouth",
    "myst",
    "blade runner",
    "syberia",
    "the neverhood",
]


def detect_scummvm_game(
    game,
    game_ids: Optional[Dict] = None,
    platform_query: Optional[PlatformDataQuery] = None,
) -> Optional[PlatformCandidate]:
    """Detect if a game should run under ScummVM.

    Signal chain:

    1. User saved platform = scummvm           → 100
    2. Seed match by store_app_id              → 95
    3. Metadata has scummvm_id                 → 95
    4. PCGamingWiki engine = "ScummVM"         → 90
    5. Tag contains "scummvm"                  → 90
    6. Seed match by normalized title          → 80
    7. Known series title match                → 75
    8. Adventure genre + pre-2000              → 40

    Args:
        game: GameEntry or compatible object
        game_ids: ScummVM game ID seed (defaults to SCUMMVM_GAME_IDS)
        platform_query: Optional cross-plugin DB query object

    Returns:
        PlatformCandidate with highest-scoring signal, or None
    """
    if game_ids is None:
        game_ids = SCUMMVM_GAME_IDS

    game_id = _get_attr(game, "id", "unknown")
    best: Optional[PlatformCandidate] = None

    def _consider(score: int, source: str, reason: str,
                  hint: dict = None) -> bool:
        nonlocal best
        if best is None or score > best.score:
            best = PlatformCandidate(
                game_id=game_id, platform="scummvm",
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
            if isinstance(config, dict) and config.get("platform") == "scummvm":
                if _consider(100, "user_saved", "User-selected ScummVM platform"):
                    return best
        except (json.JSONDecodeError, TypeError):
            pass

    # 2. Seed match by store_app_id
    store_app_ids = _get_attr(game, "store_app_ids", {}) or {}
    for store_name, store_app_id in store_app_ids.items():
        for seed_title, seed_data in game_ids.items():
            seed_store_id = seed_data.get(store_name)
            if seed_store_id and str(seed_store_id) == str(store_app_id):
                scummvm_id = seed_data["id"]
                if _consider(95, "game_id_seed",
                             f"Seed match: {seed_title}",
                             hint={"scummvm_id": scummvm_id}):
                    return best
                break

    # 3. Metadata has scummvm_id
    extra_metadata = _get_attr(game, "extra_metadata", {}) or {}
    scummvm_id = extra_metadata.get("scummvm_id")
    if scummvm_id:
        if _consider(95, "metadata",
                     f"Metadata scummvm_id: {scummvm_id}",
                     hint={"scummvm_id": scummvm_id}):
            return best

    # 4. PCGamingWiki engine = "ScummVM"
    if platform_query:
        for store_name, store_app_id in store_app_ids.items():
            engines = platform_query.get_pcgw_engines(
                store_name, str(store_app_id)
            )
            for engine in engines:
                if "scummvm" in engine.lower():
                    if _consider(90, "pcgw_engine",
                                 f"PCGamingWiki engine: {engine}"):
                        return best
                    break

    # 5. Tag contains "scummvm"
    tags = _get_attr(game, "tags", []) or []
    if isinstance(tags, str):
        tags = [tags]
    for tag in tags:
        tag_lower = tag.lower() if isinstance(tag, str) else ""
        if "scummvm" in tag_lower:
            if _consider(90, "tag_match", f"Tag: {tag}"):
                return best
            break

    # 6. Seed match by normalized title
    title = _get_attr(game, "title", "")
    if isinstance(title, str) and title:
        title_lower = title.lower()
        for seed_title, seed_data in game_ids.items():
            if seed_title in title_lower:
                scummvm_id = seed_data["id"]
                _consider(80, "game_id_seed",
                          f"Title seed match: {seed_title}",
                          hint={"scummvm_id": scummvm_id})
                break

    # 7. Known series title match
    if isinstance(title, str) and title:
        title_lower = title.lower()
        for series in SCUMMVM_SERIES:
            if series in title_lower:
                _consider(75, "tag_match",
                          f"Known ScummVM series: {series}")
                break

    # 8. Adventure genre + pre-2000
    genres = _get_attr(game, "genres", []) or []
    is_adventure = any(
        isinstance(g, str) and "adventure" in g.lower() for g in genres
    )
    if is_adventure:
        release_date = _get_attr(game, "release_date", "")
        if isinstance(release_date, str) and release_date:
            year = _extract_year(release_date)
            if year and year < 2000:
                _consider(40, "title_heuristic",
                          f"Adventure game from {year}")

    if best and best.score >= PlatformCandidate.SUGGEST:
        return best
    return None


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
