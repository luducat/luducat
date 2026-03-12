# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# game_entry.py

"""Memory-efficient game cache entry for luducat.

Replaces Dict[str, Any] in _games_cache with a __slots__-based dataclass,
eliminating ~200+ bytes of per-instance dict overhead per game.
Implements dict-like access (.get(), __getitem__, __contains__) so existing
game.get("field") call sites work unchanged.

String interning is applied at construction time in _db_game_to_ui(),
not in this class — keeps the data structure clean and interning explicit.
"""

import sys
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional

_intern = sys.intern

# Strings that repeat across 15k games — intern at construction
_INTERN_FIELDS = frozenset({
    "primary_store", "cover_source", "protondb_rating", "steam_deck_compat",
    "franchise", "normalized_title",
})
# List fields where CONTENTS should be interned (each string in the list)
_INTERN_LIST_FIELDS = frozenset({
    "stores", "developers", "publishers", "genres", "themes", "game_modes",
})


@dataclass(slots=True)
class GameEntry:
    """Memory-efficient game cache entry replacing Dict[str, Any].

    Uses __slots__ to eliminate per-instance dict overhead (~200+ bytes/game).
    Implements dict-like .get()/__getitem__ for backward compatibility.
    """
    # Identity
    id: str = ""
    title: str = ""
    normalized_title: str = ""
    primary_store: str = ""

    # Store info
    stores: list = field(default_factory=list)
    store_app_ids: dict = field(default_factory=dict)
    launch_urls: dict = field(default_factory=dict)

    # User state
    is_favorite: bool = False
    is_hidden: bool = False
    is_family_shared: bool = False
    family_license_count: int = 0
    is_installed: bool = False

    # Tags
    tags: list = field(default_factory=list)

    # Timestamps & usage
    added_at: Optional[str] = None
    last_launched: Optional[str] = None
    launch_count: int = 0
    playtime_minutes: int = 0
    notes: str = ""

    # Display metadata
    short_description: str = ""
    header_image: str = ""
    cover_image: str = ""
    screenshots: list = field(default_factory=list)
    release_date: str = ""
    developers: list = field(default_factory=list)
    publishers: list = field(default_factory=list)
    genres: list = field(default_factory=list)
    franchise: str = ""
    game_modes: list = field(default_factory=list)
    themes: list = field(default_factory=list)

    # Flags
    is_free: bool = False
    is_demo: bool = False

    # Compatibility
    protondb_rating: str = ""
    steam_deck_compat: str = ""

    # Content filter
    adult_confidence: float = 0.0
    nsfw_override: int = 0

    # Player counts (from game_modes_detail)
    online_players: str = ""
    local_players: str = ""
    lan_players: str = ""

    # Cover source
    cover_source: str = ""

    # Per-game launch config (JSON string)
    launch_config: str = ""

    # --- Dict compatibility shim ---

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-compatible .get() for backward compatibility."""
        try:
            return getattr(self, key)
        except AttributeError:
            return default

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def __setitem__(self, key: str, value: Any) -> None:
        try:
            setattr(self, key, value)
        except AttributeError:
            raise KeyError(f"GameEntry has no field '{key}'") from None

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return hasattr(self, key)

    def update(self, d: dict) -> None:
        """Dict-compatible .update() for backward compatibility."""
        for key, value in d.items():
            try:
                setattr(self, key, value)
            except AttributeError:
                pass  # Skip unknown keys silently

    def __iter__(self):
        """Iterate over field names (dict-like key iteration)."""
        return (f.name for f in fields(self))

    def items(self):
        """Dict-compatible .items() returning (key, value) pairs."""
        return [(f.name, getattr(self, f.name)) for f in fields(self)]

    def keys(self):
        """Dict-compatible .keys() returning field names."""
        return [f.name for f in fields(self)]

    def values(self):
        """Dict-compatible .values() returning field values."""
        return [getattr(self, f.name) for f in fields(self)]

    @classmethod
    def field_names(cls) -> frozenset:
        """Return frozenset of all field names."""
        return frozenset(f.name for f in fields(cls))

    @classmethod
    def from_dict(cls, d: dict) -> "GameEntry":
        """Construct from a dict, ignoring unknown keys."""
        valid = cls.field_names()
        return cls(**{k: v for k, v in d.items() if k in valid})
