# Quickstart: Build Your First Plugin in 10 Minutes

This guide creates a minimal store plugin called **GameVault** that adds three
hardcoded games to your library. No API calls, no database -- just the bare
minimum to see games appear in luducat.

## Prerequisites

- luducat installed and running
- Python 3.11+
- A text editor

## Step 1: Create the Plugin Directory

Find your plugins directory:
- Linux: `~/.local/share/luducat/plugins/`
- Windows: `%APPDATA%/luducat/plugins/`
- macOS: `~/Library/Application Support/luducat/plugins/`

Create a new directory:

```bash
mkdir -p ~/.local/share/luducat/plugins/gamevault
cd ~/.local/share/luducat/plugins/gamevault
```

## Step 2: Write plugin.json

Create `plugin.json`:

```json
{
  "name": "gamevault",
  "display_name": "GameVault",
  "version": "0.1.0",
  "author": "Your Name",
  "description": "My first luducat plugin",
  "min_luducat_version": "0.2.9.24",
  "plugin_types": ["store"],
  "store_class": "store.GameVaultStore",
  "badge_label": "GV",
  "brand_colors": {"bg": "#2a4a2a", "text": "#90ee90"},
  "capabilities": {
    "fetch_library": true,
    "fetch_metadata": true,
    "launch_games": false
  },
  "provides_fields": {
    "title": {"priority": 50},
    "description": {"priority": 50}
  },
  "auth": {"type": "none"},
  "network": {"allowed_domains": []},
  "privacy": {"telemetry": false, "data_collection": "none", "third_party_services": []},
  "settings_schema": {}
}
```

## Step 3: Write the Store Class

Create `store.py`:

```python
from pathlib import Path
from typing import List, Optional

from luducat.plugins.base import AbstractGameStore, Game


class GameVaultStore(AbstractGameStore):
    """A minimal store plugin with hardcoded games."""

    # Our fake game catalog
    _GAMES = {
        "1": {
            "title": "Cybernetic Uprising",
            "description": "A cyberpunk RPG set in Neo-Tokyo 2099.",
            "genres": ["RPG", "Action"],
            "release_date": "2024-03-15",
            "developers": ["Neon Forge Studios"],
        },
        "2": {
            "title": "Starfield Wanderer",
            "description": "Explore a procedurally generated galaxy.",
            "genres": ["Adventure", "Simulation"],
            "release_date": "2023-09-01",
            "developers": ["Cosmos Interactive"],
        },
        "3": {
            "title": "Castle of Shadows",
            "description": "A gothic metroidvania with hand-drawn art.",
            "genres": ["Platformer", "Action"],
            "release_date": "2025-01-20",
            "developers": ["Moonlit Games"],
        },
    }

    @property
    def store_name(self) -> str:
        return "gamevault"

    @property
    def display_name(self) -> str:
        return "GameVault"

    def is_available(self) -> bool:
        return True  # Always available (no external dependencies)

    def is_authenticated(self) -> bool:
        return True  # No auth needed

    async def authenticate(self) -> bool:
        return True

    async def fetch_user_games(self, status_callback=None,
                                cancel_check=None) -> List[str]:
        if status_callback:
            status_callback("Loading GameVault library...")
        return list(self._GAMES.keys())

    async def fetch_game_metadata(self, app_ids, download_images=False):
        games = []
        for app_id in app_ids:
            data = self._GAMES.get(app_id)
            if data:
                games.append(Game(
                    store_app_id=app_id,
                    store_name=self.store_name,
                    title=data["title"],
                    launch_url="",
                    description=data.get("description"),
                    genres=data.get("genres", []),
                    release_date=data.get("release_date"),
                    developers=data.get("developers", []),
                ))
        return games

    def get_database_path(self) -> Path:
        return self.data_dir / "catalog.db"

    def get_game_metadata(self, app_id: str) -> Optional[dict]:
        return self._GAMES.get(app_id)
```

## Step 4: Write \_\_init\_\_.py

Create `__init__.py`:

```python
from .store import GameVaultStore

__all__ = ["GameVaultStore"]
```

## Step 5: Launch and Test

1. Start luducat
2. Go to **Settings > Plugins**
3. Enable "GameVault" in the sidebar
4. Click **Sync** in the toolbar
5. Three games should appear in your library with green "GV" badges

## What Happened

1. Luducat discovered `gamevault/plugin.json` during startup
2. It loaded `GameVaultStore` from `store.py`
3. During sync, it called `fetch_user_games()` to get `["1", "2", "3"]`
4. Then `fetch_game_metadata()` to get the `Game` objects
5. The games were added to the main database and displayed in the UI

## Next Steps

### Add a Real API

Replace the hardcoded `_GAMES` dict with actual HTTP calls:

```python
async def fetch_user_games(self, **kwargs):
    resp = self.http.get("https://api.gamevault.com/library", timeout=10)
    resp.raise_for_status()
    return [str(g["id"]) for g in resp.json()["games"]]
```

Don't forget to add the domain to `plugin.json`:

```json
{"network": {"allowed_domains": ["api.gamevault.com"]}}
```

### Add Authentication

Switch from `"auth": {"type": "none"}` to `"api_key"` and implement
`is_authenticated()` / `authenticate()` properly. See the
[Authentication](sdk/v1/docs/authentication.md) guide.

### Add a Database

Use SQLAlchemy for a proper catalog database instead of a hardcoded dict.
See the [Store Plugin](sdk/v1/docs/store-plugin.md) guide.

### Add Images

Return `cover_image_url` and `header_image_url` in your `Game` objects.
Luducat handles downloading and caching automatically.

### Write Tests

Create `tests/test_store.py` with pytest. See the
[Testing](sdk/v1/docs/testing.md) guide.

## Troubleshooting

**Plugin doesn't appear in Settings:**
- Check the log file (`~/.local/share/luducat/luducat.log`) for load errors
- Verify `plugin.json` is valid JSON (no trailing commas)
- Check `min_luducat_version` matches your installed version

**Plugin is blocked:**
- Check the log for "Import audit" messages
- Ensure you're not importing from `luducat.core.*`

**Games don't appear after sync:**
- Check that `fetch_user_games()` returns non-empty list
- Check that `fetch_game_metadata()` returns `Game` objects
- Check the sync log for errors
