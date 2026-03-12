"""GameVault -- Demo store plugin for the luducat Plugin SDK.

This plugin demonstrates a complete store implementation using a fictional
game store with hardcoded data. No real API calls are made.

Key concepts demonstrated:
- All required AbstractGameStore methods
- API key authentication via credential helpers
- Game dataclass construction with rich metadata
- Status callbacks and cancel checking during sync
- Config actions (dynamic settings dialog buttons)
- Lifecycle hooks (on_enable, on_disable, close)
- Store page URL generation
- Launch URL scheme pattern
"""

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from luducat.plugins.base import (
    AbstractGameStore,
    AuthenticationError,
    ConfigAction,
    Game,
)

logger = logging.getLogger(__name__)


# Hardcoded game catalog -- in a real plugin this comes from an API
_CATALOG: Dict[str, Dict[str, Any]] = {
    "gv-1001": {
        "title": "Neon Circuit",
        "short_description": "Race through a neon-lit cyberpunk city.",
        "description": (
            "<p>Neon Circuit is a high-speed racing game set in a sprawling "
            "cyberpunk metropolis. Customize your vehicle with cybernetic "
            "upgrades and compete in underground racing leagues.</p>"
        ),
        "genres": ["Racing", "Action"],
        "developers": ["Synth Drive Studios"],
        "publishers": ["GameVault Publishing"],
        "release_date": "2024-06-15",
        "cover_url": "https://cdn.gamevault.example.com/covers/gv-1001.jpg",
        "screenshots": [
            "https://cdn.gamevault.example.com/screens/gv-1001-1.jpg",
            "https://cdn.gamevault.example.com/screens/gv-1001-2.jpg",
        ],
    },
    "gv-1002": {
        "title": "Echoes of Eternity",
        "short_description": "An epic RPG spanning multiple timelines.",
        "description": (
            "<p>Journey through three interconnected timelines in this "
            "narrative-driven RPG. Your choices ripple across centuries.</p>"
        ),
        "genres": ["RPG", "Adventure"],
        "developers": ["Temporal Games"],
        "publishers": ["GameVault Publishing"],
        "release_date": "2023-11-20",
        "cover_url": "https://cdn.gamevault.example.com/covers/gv-1002.jpg",
        "screenshots": [],
    },
    "gv-1003": {
        "title": "Fungi Frontier",
        "short_description": "Build a mushroom civilization from spores to space.",
        "description": (
            "<p>A colony management sim where you guide a fungal civilization "
            "from humble spores to an interstellar empire. Features real-time "
            "mycelium network simulation.</p>"
        ),
        "genres": ["Strategy", "Simulation"],
        "developers": ["Mycelium Works", "Spore Labs"],
        "publishers": ["GameVault Publishing"],
        "release_date": "2025-02-01",
        "cover_url": "https://cdn.gamevault.example.com/covers/gv-1003.jpg",
        "screenshots": [
            "https://cdn.gamevault.example.com/screens/gv-1003-1.jpg",
        ],
    },
    "gv-1004": {
        "title": "Obsidian Depths",
        "short_description": "A roguelike dungeon crawler with crafting.",
        "description": (
            "<p>Descend into procedurally generated dungeons, craft weapons "
            "from obsidian and bone, and face ancient horrors. Permadeath "
            "with persistent unlocks between runs.</p>"
        ),
        "genres": ["Roguelike", "RPG", "Action"],
        "developers": ["Deep Forge Games"],
        "publishers": ["Deep Forge Games"],
        "release_date": "2024-09-30",
        "cover_url": "https://cdn.gamevault.example.com/covers/gv-1004.jpg",
        "screenshots": [
            "https://cdn.gamevault.example.com/screens/gv-1004-1.jpg",
            "https://cdn.gamevault.example.com/screens/gv-1004-2.jpg",
            "https://cdn.gamevault.example.com/screens/gv-1004-3.jpg",
        ],
    },
    "gv-1005": {
        "title": "Windborne Tales",
        "short_description": "A cozy exploration game about a traveling bard.",
        "genres": ["Adventure", "Indie"],
        "developers": ["Zephyr Interactive"],
        "publishers": ["Zephyr Interactive"],
        "release_date": "2025-04-10",
        # Minimal metadata -- demonstrates partial data handling
    },
}

# Simulated user library (subset of catalog)
_USER_LIBRARY = ["gv-1001", "gv-1002", "gv-1003", "gv-1004", "gv-1005"]


class GameVaultStore(AbstractGameStore):
    """GameVault store plugin.

    Demonstrates all required and several optional methods of the
    AbstractGameStore base class using hardcoded data.
    """

    # ── Required Properties ──────────────────────────────────────────

    @property
    def store_name(self) -> str:
        return "gamevault"

    @property
    def display_name(self) -> str:
        return "GameVault"

    # ── Required Methods ─────────────────────────────────────────────

    def is_available(self) -> bool:
        # A real plugin might check if the store client is installed
        return True

    def is_authenticated(self) -> bool:
        # Check if we have a stored API key
        return bool(self.get_credential("api_key"))

    async def authenticate(self) -> bool:
        api_key = self.get_credential("api_key")
        if not api_key:
            raise AuthenticationError(
                "GameVault API key not configured. "
                "Enter any non-empty string in Settings for this demo."
            )

        # In a real plugin: validate the key against the API
        # resp = self.http.get(
        #     "https://api.gamevault.example.com/v1/validate",
        #     headers={"Authorization": f"Bearer {api_key}"},
        #     timeout=10,
        # )
        # resp.raise_for_status()

        logger.info("GameVault: authenticated (demo mode)")
        return True

    async def fetch_user_games(
        self,
        status_callback: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> List[str]:
        if status_callback:
            status_callback("Fetching GameVault library...")

        # In a real plugin: paginated API call
        # all_ids = []
        # for page in range(1, total_pages + 1):
        #     if cancel_check and cancel_check():
        #         break
        #     resp = self.http.get(...)
        #     ...

        # Check cancel between operations
        if cancel_check and cancel_check():
            return []

        if status_callback:
            status_callback(f"Found {len(_USER_LIBRARY)} games")

        return list(_USER_LIBRARY)

    async def fetch_game_metadata(
        self,
        app_ids: List[str],
        download_images: bool = False,
    ) -> List[Game]:
        games = []

        for app_id in app_ids:
            data = _CATALOG.get(app_id)
            if not data:
                logger.warning("GameVault: unknown app_id %s", app_id)
                continue

            game = Game(
                store_app_id=app_id,
                store_name=self.store_name,
                title=data["title"],
                launch_url=f"gamevault://play/{app_id}",
                short_description=data.get("short_description"),
                description=data.get("description"),
                cover_image_url=data.get("cover_url"),
                screenshots=data.get("screenshots", []),
                release_date=data.get("release_date"),
                developers=data.get("developers", []),
                publishers=data.get("publishers", []),
                genres=data.get("genres", []),
            )
            games.append(game)

        return games

    def get_database_path(self) -> Path:
        return self.data_dir / "catalog.db"

    # ── Optional Methods ─────────────────────────────────────────────

    def get_game_metadata(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Get metadata for one game from the catalog."""
        return _CATALOG.get(app_id)

    def get_games_metadata_bulk(
        self, app_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Batch metadata lookup."""
        return {
            aid: _CATALOG[aid]
            for aid in app_ids
            if aid in _CATALOG
        }

    def get_store_page_url(self, app_id: str) -> str:
        return f"https://gamevault.example.com/game/{app_id}"

    def get_config_actions(self) -> List[ConfigAction]:
        """Dynamic config dialog buttons."""
        return [
            ConfigAction(
                id="test_connection",
                label="Test Connection",
                callback="_test_connection",
                group="auth",
                requires_auth=True,
                tooltip="Verify API key is valid",
            ),
        ]

    def _test_connection(self):
        """Called when 'Test Connection' button is clicked."""
        logger.info("GameVault: connection test -- demo always succeeds")

    # ── Lifecycle Hooks ──────────────────────────────────────────────

    def on_enable(self) -> None:
        logger.info("GameVault plugin enabled")

    def on_disable(self) -> None:
        logger.info("GameVault plugin disabled")

    def close(self) -> None:
        logger.info("GameVault plugin shutting down")
