# Building a Store Plugin

This guide walks through implementing a complete store plugin. Store plugins
import game libraries from storefronts and provide game metadata.

**Base class:** `AbstractGameStore` (alias: `StorePlugin`)

## Skeleton

```python
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from luducat.plugins.base import (
    AbstractGameStore,
    AuthenticationError,
    Game,
    NetworkError,
    RateLimitError,
)


class MyStore(AbstractGameStore):
    """My game store integration."""

    @property
    def store_name(self) -> str:
        return "my_store"

    @property
    def display_name(self) -> str:
        return "My Store"

    def is_available(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return bool(self.get_credential("api_key"))

    async def authenticate(self) -> bool:
        api_key = self.get_credential("api_key")
        if not api_key:
            raise AuthenticationError("API key not configured")
        return True

    async def fetch_user_games(self, status_callback=None,
                                cancel_check=None) -> List[str]:
        return []

    async def fetch_game_metadata(self, app_ids, download_images=False):
        return []

    def get_database_path(self) -> Path:
        return self.data_dir / "catalog.db"
```

## Required Properties

### `store_name -> str`

Unique identifier. Lowercase, alphanumeric + underscores. Used as database
prefix, config key, and credential namespace.

```python
@property
def store_name(self) -> str:
    return "my_store"
```

### `display_name -> str`

Human-readable name shown in the UI:

```python
@property
def display_name(self) -> str:
    return "My Store"
```

## Required Methods

### `is_available() -> bool`

Check if the store can be used on this system. Runs at startup:

```python
def is_available(self) -> bool:
    # Always available (web-based store)
    return True

# Or check for a local client:
def is_available(self) -> bool:
    return Path("~/.config/my_store").expanduser().exists()
```

### `is_authenticated() -> bool`

Check if valid credentials exist:

```python
def is_authenticated(self) -> bool:
    return bool(self.get_credential("api_key"))
```

### `authenticate() -> bool`

Perform the authentication flow. Called before sync:

```python
async def authenticate(self) -> bool:
    api_key = self.get_credential("api_key")
    if not api_key:
        raise AuthenticationError("API key not configured")

    resp = self.http.get(
        "https://api.mystore.com/v1/validate",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    if resp.status_code == 401:
        raise AuthenticationError("Invalid API key")
    if resp.status_code == 429:
        raise RateLimitError(wait_seconds=60)
    resp.raise_for_status()
    return True
```

### `fetch_user_games(status_callback, cancel_check) -> List[str]`

Fetch the list of game IDs the user owns. This should be fast -- just IDs,
not full metadata:

```python
async def fetch_user_games(self, status_callback=None,
                            cancel_check=None) -> List[str]:
    api_key = self.get_credential("api_key")
    all_ids = []
    page = 1

    while True:
        if cancel_check and cancel_check():
            break

        resp = self.http.get(
            "https://api.mystore.com/v1/library",
            params={"page": page, "per_page": 100},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        ids = [str(g["id"]) for g in data["games"]]
        all_ids.extend(ids)

        if status_callback:
            status_callback(f"Fetched {len(all_ids)} games...")

        if not data.get("has_more"):
            break
        page += 1

    return all_ids
```

### `fetch_game_metadata(app_ids, download_images) -> List[Game]`

Fetch detailed metadata for the given IDs. Return `Game` dataclass instances:

```python
async def fetch_game_metadata(self, app_ids, download_images=False):
    games = []
    for app_id in app_ids:
        try:
            resp = self.http.get(
                f"https://api.mystore.com/v1/games/{app_id}",
                timeout=15,
            )
            if resp.status_code == 429:
                raise RateLimitError(wait_seconds=60)
            resp.raise_for_status()
            data = resp.json()

            game = Game(
                store_app_id=str(data["id"]),
                store_name=self.store_name,
                title=data["title"],
                launch_url=f"mystore://play/{data['id']}",
                short_description=data.get("summary"),
                description=data.get("description"),
                cover_image_url=data.get("cover_url"),
                header_image_url=data.get("header_url"),
                screenshots=data.get("screenshots", []),
                release_date=data.get("release_date"),
                developers=data.get("developers", []),
                publishers=data.get("publishers", []),
                genres=data.get("genres", []),
            )
            games.append(game)

        except RateLimitError:
            raise  # Let sync system handle it
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to fetch {app_id}: {e}"
            )
    return games
```

### `get_database_path() -> Path`

Return the path to your catalog database:

```python
def get_database_path(self) -> Path:
    return self.data_dir / "catalog.db"
```

## Optional Methods

### Metadata Lookup

Implement these for the metadata priority system to work:

```python
def get_game_metadata(self, app_id: str) -> Optional[Dict[str, Any]]:
    """Get metadata for one game from your local database."""
    # Query your catalog.db
    ...

def get_games_metadata_bulk(self, app_ids: List[str]) -> Dict[str, Dict]:
    """Batch version -- much faster for 15k+ games."""
    # Single SQL query with IN clause
    ...

def get_metadata_for_store_game(self, store_name, store_id,
                                 normalized_title=""):
    """Cross-store metadata lookup."""
    if store_name == self.store_name:
        return self.get_game_metadata(store_id)
    if normalized_title:
        return self._find_game_by_title(normalized_title)
    return None
```

### Store Page URL

```python
def get_store_page_url(self, app_id: str) -> str:
    return f"https://mystore.com/game/{app_id}"
```

### Account Identity

Used for ownership reconciliation (detecting account changes between syncs):

```python
def get_account_identifier(self) -> Optional[str]:
    return self.get_credential("user_id")
```

### Tag Sync

If your store has user-defined tags/categories:

```python
def get_tag_sync_data(self) -> Optional[Dict[str, Any]]:
    """Return tag data from the store.

    Returns:
        Dict mapping store_app_id -> {
            "tags": ["Action", "RPG"],
            "favorite": True/False,
            "hidden": True/False,
        }
    """
    ...
```

### Playtime Sync

```python
def get_playtime_sync_data(self) -> Optional[Dict[str, Any]]:
    """Return playtime data.

    Returns:
        Dict mapping store_app_id -> {
            "minutes": 1234,
            "last_played": "2025-06-15T10:30:00",
        }
    """
    ...
```

### Installation Status

```python
def get_install_sync_data(self) -> Optional[Dict[str, Any]]:
    """Return installation status.

    Returns:
        Dict mapping store_app_id -> {
            "installed": True,
            "install_path": "/path/to/game",
        }
    """
    ...
```

### Config Actions

Dynamic buttons for the settings dialog:

```python
from luducat.plugins.base import ConfigAction

def get_config_actions(self) -> List[ConfigAction]:
    return [
        ConfigAction(
            id="test_connection",
            label="Test Connection",
            callback="_test_connection",
            group="auth",
            icon="refresh.svg",
            requires_auth=True,
        ),
    ]

def _test_connection(self):
    """Called when the button is clicked."""
    ...
```

### Lifecycle Hooks

```python
def on_enable(self):
    """Called when plugin is enabled. Initialize databases, etc."""
    ...

def on_disable(self):
    """Called when plugin is disabled. Clean up resources."""
    ...

def on_sync_complete(self, progress_callback=None):
    """Called after sync. Post-processing, cleanup."""
    return {"games_updated": 42}

def close(self):
    """Called on app shutdown. Close DB connections, etc."""
    ...
```

## Injected Properties

These are available in all methods (not in `__init__`):

| Property | Type | Usage |
|----------|------|-------|
| `self.http` | `PluginHttpClient` | `self.http.get(url, timeout=10)` |
| `self.storage` | `PluginStorage` | `self.storage.write_text("state.json", data)` |
| `self.main_db` | `MainDbAccessor` | `self.main_db.get_store_game(...)` |
| `self.config_dir` | `Path` | Plugin config directory |
| `self.cache_dir` | `Path` | Plugin cache directory |
| `self.data_dir` | `Path` | Plugin data directory |

Credential helpers: `self.get_credential(key)`, `self.set_credential(key, value)`,
`self.delete_credential(key)`

Settings: `self.get_setting(key, default=None)`

Privacy: `self.has_local_data_consent()`

## The Game Dataclass

Every game returned from `fetch_game_metadata()` must be a `Game` instance:

```python
from luducat.plugins.base import Game

game = Game(
    # Required
    store_app_id="12345",
    store_name="my_store",
    title="Portal 2",
    launch_url="mystore://play/12345",

    # Optional metadata
    short_description="A puzzle game",
    description="<p>Full HTML description...</p>",
    header_image_url="https://cdn.mystore.com/12345/header.jpg",
    cover_image_url="https://cdn.mystore.com/12345/cover.jpg",
    background_image_url="https://cdn.mystore.com/12345/bg.jpg",
    screenshots=["https://cdn.mystore.com/12345/ss1.jpg"],
    release_date="2011-04-19",
    developers=["Valve"],
    publishers=["Valve"],
    genres=["Puzzle", "Action"],
    categories=["Single-player", "Co-op"],
    tags=["Puzzle", "Science", "Comedy"],
    playtime_minutes=1234,
    extra_metadata={"my_store_rating": 98},
)
```

## Database Pattern

Store plugins typically use SQLAlchemy for their catalog database:

```python
from sqlalchemy import create_engine, Column, String, Text
from sqlalchemy.orm import declarative_base, Session

Base = declarative_base()

class CatalogGame(Base):
    __tablename__ = "games"
    app_id = Column(String, primary_key=True)
    title = Column(String, nullable=False)
    metadata_json = Column(Text)  # JSON blob for flexible metadata

def _get_engine(self):
    db_path = self.get_database_path()
    return create_engine(f"sqlite:///{db_path}")
```

Use `selectinload` (not `joinedload`) for one-to-many relationships.

## Checklist

Before shipping your store plugin:

- [ ] All 7 required methods implemented
- [ ] `plugin.json` complete with all sections
- [ ] `provides_fields` declares metadata capabilities
- [ ] `network.allowed_domains` lists all accessed domains
- [ ] `privacy.telemetry` is `false`
- [ ] Credentials stored via `set_credential()`, not in files
- [ ] Rate limits handled (raise `RateLimitError`, don't sleep)
- [ ] Cancel check respected in long-running operations
- [ ] Tests cover authentication, fetch, and error paths
- [ ] Version bumped on every change
