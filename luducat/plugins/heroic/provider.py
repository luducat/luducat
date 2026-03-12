# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# provider.py

"""Heroic Games Metadata Provider.

Imports tags (custom categories) and favourites from Heroic's local
JSON config into luducat. No network access — reads only local files.

Supported data:
- Custom categories → luducat tags (source="heroic", tag_type="imported")
- Favourites → luducat is_favorite flag

Config paths:
- Native:  ~/.config/heroic/
- Flatpak: ~/.var/app/com.heroicgameslauncher.hgl/config/heroic/
"""

import logging
import platform
from pathlib import Path
from typing import Any, Dict, List, Optional

from luducat.plugins.base import AbstractMetadataProvider, EnrichmentData, MetadataSearchResult

logger = logging.getLogger(__name__)

# Heroic store codes → luducat store names
_STORE_CODE_MAP = {
    "gog": "gog",
    "epic": "epic",
    # "nile" → future Amazon plugin, skip for now
    # "sideload" → no luducat store, skip
}


class HeroicProvider(AbstractMetadataProvider):
    """Import tags and favourites from Heroic."""

    # ── Identity ──────────────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "heroic"

    @property
    def display_name(self) -> str:
        return "Heroic"

    # ── Availability ──────────────────────────────────────────────────

    def _find_heroic_config_dir(self) -> Optional[Path]:
        """Locate Heroic's config directory (native or Flatpak).

        Returns:
            Path to the heroic config root, or None if not found.
        """
        system = platform.system()

        if system == "Linux":
            candidates = [
                Path.home() / ".config" / "heroic",
                Path.home() / ".var" / "app" / "com.heroicgameslauncher.hgl"
                / "config" / "heroic",
            ]
        elif system == "Windows":
            appdata = Path.home() / "AppData" / "Roaming" / "heroic"
            candidates = [appdata]
        elif system == "Darwin":
            candidates = [
                Path.home() / "Library" / "Application Support" / "heroic",
            ]
        else:
            candidates = []

        for path in candidates:
            if path.is_dir():
                return path
        return None

    def is_available(self) -> bool:
        config_dir = self._find_heroic_config_dir()
        if not config_dir:
            return False
        return (config_dir / "store" / "config.json").is_file()

    # ── Auth (not needed) ─────────────────────────────────────────────

    async def authenticate(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return True

    # ── Tag sync ──────────────────────────────────────────────────────

    def get_tag_sync_data(self, **kwargs) -> Optional[Dict[str, Any]]:
        """Read Heroic config and return tag/favourite sync data.

        Keyword Args:
            import_favourites: Override for import_favourites setting
                (from centralized tags config).

        Returns:
            Dict with "source", "mode", "entries" or None on failure.
        """
        if not self.has_local_data_consent():
            logger.debug("Skipping Heroic tag sync: local data consent not granted")
            return None

        config_dir = self._find_heroic_config_dir()
        if not config_dir:
            return None

        config_path = config_dir / "store" / "config.json"
        if not config_path.is_file():
            return None

        try:
            config = self._read_json(config_path)
        except Exception as e:
            logger.warning(f"Failed to read Heroic config: {e}")
            return None

        games_block = config.get("games", {})
        if not isinstance(games_block, dict):
            return None

        # Build title lookup from library caches
        title_lookup = self._build_title_lookup(config_dir)

        # Parse custom categories
        categories = games_block.get("customCategories", {})
        entries = self._parse_custom_categories(categories, title_lookup)

        # Parse favourites (centralized config overrides plugin setting)
        import_favs = kwargs.get(
            "import_favourites",
            self.get_setting("import_favourites", True),
        )
        if import_favs:
            favourites = games_block.get("favourites", [])
            fav_entries = self._parse_favourites(
                favourites, config_dir, title_lookup,
            )
            # Merge fav_entries into entries
            entries = self._merge_entries(entries, fav_entries)

        if not entries:
            return None

        return {
            "source": "heroic",
            "mode": "add_only",
            "entries": list(entries.values()),
        }

    # ── Internal parsing ──────────────────────────────────────────────

    def _read_json(self, path: Path) -> Any:
        """Read and parse a JSON file."""
        from luducat.plugins.sdk.json import json
        with open(path, "r", encoding="utf-8") as f:
            return json.loads(f.read())

    def _build_title_lookup(self, config_dir: Path) -> Dict[str, str]:
        """Build app_name → title mapping from Heroic library caches.

        Reads gog_library.json and legendary_library.json (Epic).

        Returns:
            Dict mapping bare app_name → title
        """
        lookup: Dict[str, str] = {}

        # GOG library
        gog_lib = config_dir / "gog_store" / "library.json"
        if gog_lib.is_file():
            try:
                data = self._read_json(gog_lib)
                games = data.get("games", data) if isinstance(data, dict) else data
                if isinstance(games, list):
                    for game in games:
                        app_name = game.get("app_name", "")
                        title = game.get("title", "")
                        if app_name and title:
                            lookup[app_name] = title
            except Exception as e:
                logger.debug(f"Failed to read GOG library cache: {e}")

        # Epic (Legendary) library
        epic_lib = config_dir / "legendary_store" / "library.json"
        if not epic_lib.is_file():
            # Fallback: some installs put it under store/
            epic_lib = config_dir / "store" / "legendary_library.json"
        if epic_lib.is_file():
            try:
                data = self._read_json(epic_lib)
                games = data.get("library", data) if isinstance(data, dict) else data
                if isinstance(games, list):
                    for game in games:
                        app_name = game.get("app_name", "")
                        title = game.get("title", "")
                        if app_name and title:
                            lookup[app_name] = title
            except Exception as e:
                logger.debug(f"Failed to read Epic library cache: {e}")

        return lookup

    def _parse_custom_categories(
        self,
        categories: Dict[str, List[str]],
        title_lookup: Dict[str, str],
    ) -> Dict[str, Dict[str, Any]]:
        """Parse customCategories block into entry dicts.

        Args:
            categories: {"CategoryName": ["appid_store", ...], ...}
            title_lookup: app_name → title mapping

        Returns:
            Dict keyed by "store:app_id" → entry dict
        """
        entries: Dict[str, Dict[str, Any]] = {}

        if not isinstance(categories, dict):
            return entries

        for category_name, members in categories.items():
            if not isinstance(members, list):
                continue
            for member in members:
                if not isinstance(member, str):
                    continue
                store, app_id = self._split_heroic_id(member)
                if not store or not app_id:
                    continue

                key = f"{store}:{app_id}"
                if key not in entries:
                    title = title_lookup.get(app_id, "")
                    entries[key] = {
                        "store": store,
                        "app_id": app_id,
                        "title": title,
                        "tags": [],
                        "favorite": False,
                    }
                if category_name not in entries[key]["tags"]:
                    entries[key]["tags"].append(category_name)

        return entries

    def _parse_favourites(
        self,
        favourites: List[Dict[str, str]],
        config_dir: Path,
        title_lookup: Dict[str, str],
    ) -> Dict[str, Dict[str, Any]]:
        """Parse favourites array into entry dicts.

        Favourites have appName but no store suffix. We resolve the store
        by checking which library cache contains the app_name.

        Args:
            favourites: [{"appName": "123", "title": "Game"}, ...]
            config_dir: Heroic config root
            title_lookup: app_name → title mapping

        Returns:
            Dict keyed by "store:app_id" → entry dict
        """
        entries: Dict[str, Dict[str, Any]] = {}

        if not isinstance(favourites, list):
            return entries

        # Build reverse lookup: app_name → store code
        store_lookup = self._build_store_lookup(config_dir)

        for fav in favourites:
            if not isinstance(fav, dict):
                continue
            app_name = fav.get("appName", "")
            title = fav.get("title", "") or title_lookup.get(app_name, "")
            if not app_name:
                continue

            store = store_lookup.get(app_name)
            if not store:
                # Unknown store — skip (could be sideload, nile, etc.)
                logger.debug(
                    f"Skipping Heroic favourite '{title}' ({app_name}): "
                    f"store not resolved"
                )
                continue

            key = f"{store}:{app_name}"
            if key not in entries:
                entries[key] = {
                    "store": store,
                    "app_id": app_name,
                    "title": title,
                    "tags": [],
                    "favorite": True,
                }
            else:
                entries[key]["favorite"] = True

        return entries

    def _build_store_lookup(self, config_dir: Path) -> Dict[str, str]:
        """Build app_name → luducat store name mapping from library caches.

        Returns:
            Dict mapping bare app_name → luducat store name ("gog", "epic")
        """
        lookup: Dict[str, str] = {}

        # GOG
        gog_lib = config_dir / "gog_store" / "library.json"
        if gog_lib.is_file():
            try:
                data = self._read_json(gog_lib)
                games = data.get("games", data) if isinstance(data, dict) else data
                if isinstance(games, list):
                    for game in games:
                        app_name = game.get("app_name", "")
                        if app_name:
                            lookup[app_name] = "gog"
            except Exception:
                pass

        # Epic (Legendary)
        epic_lib = config_dir / "legendary_store" / "library.json"
        if not epic_lib.is_file():
            epic_lib = config_dir / "store" / "legendary_library.json"
        if epic_lib.is_file():
            try:
                data = self._read_json(epic_lib)
                games = data.get("library", data) if isinstance(data, dict) else data
                if isinstance(games, list):
                    for game in games:
                        app_name = game.get("app_name", "")
                        if app_name:
                            lookup[app_name] = "epic"
            except Exception:
                pass

        return lookup

    @staticmethod
    def _split_heroic_id(heroic_id: str) -> tuple:
        """Split a Heroic game ID like '1350378876_gog' into (store, app_id).

        Args:
            heroic_id: String like "1350378876_gog" or "abc123_epic"

        Returns:
            (luducat_store_name, app_id) or ("", "") if unrecognized
        """
        # Find the last underscore — store code is the suffix
        idx = heroic_id.rfind("_")
        if idx <= 0:
            return ("", "")

        app_id = heroic_id[:idx]
        store_code = heroic_id[idx + 1:]

        luducat_store = _STORE_CODE_MAP.get(store_code)
        if not luducat_store:
            return ("", "")

        return (luducat_store, app_id)

    @staticmethod
    def _merge_entries(
        base: Dict[str, Dict[str, Any]],
        overlay: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Merge overlay entries into base, combining tags and favourites."""
        for key, entry in overlay.items():
            if key in base:
                if entry.get("favorite"):
                    base[key]["favorite"] = True
                for tag in entry.get("tags", []):
                    if tag not in base[key]["tags"]:
                        base[key]["tags"].append(tag)
                # Fill title if missing
                if not base[key]["title"] and entry.get("title"):
                    base[key]["title"] = entry["title"]
            else:
                base[key] = entry
        return base

    # ── Stub enrichment methods (not used by this plugin) ─────────────

    async def lookup_by_store_id(
        self, store_name: str, store_id: str
    ) -> Optional[str]:
        return None

    async def search_game(
        self, title: str, year: Optional[int] = None
    ) -> List[MetadataSearchResult]:
        return []

    async def get_enrichment(
        self, provider_id: str
    ) -> Optional[EnrichmentData]:
        return None

    def get_database_path(self) -> Path:
        # No database — return a dummy path that won't be created
        return self.data_dir / "heroic.db"
