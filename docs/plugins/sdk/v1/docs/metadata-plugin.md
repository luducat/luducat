# Building a Metadata Plugin

Metadata plugins enrich game data with information that store plugins don't
provide. They have no concept of game ownership -- they're pure data sources.

**Base class:** `AbstractMetadataProvider` (alias: `MetadataPlugin`)

## Skeleton

```python
from pathlib import Path
from typing import List, Optional

from luducat.plugins.base import (
    AbstractMetadataProvider,
    EnrichmentData,
    MetadataSearchResult,
)


class MyProvider(AbstractMetadataProvider):
    """My metadata provider."""

    @property
    def provider_name(self) -> str:
        return "my_provider"

    @property
    def display_name(self) -> str:
        return "My Provider"

    def is_available(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return True

    async def authenticate(self) -> bool:
        return True

    async def lookup_by_store_id(self, store_name, store_id):
        return None

    async def search_game(self, title, year=None):
        return []

    async def get_enrichment(self, provider_id):
        return None

    def get_database_path(self) -> Path:
        return self.data_dir / "enrichment.db"
```

## Required Properties

### `provider_name -> str`

Unique identifier. Lowercase, alphanumeric + underscores:

```python
@property
def provider_name(self) -> str:
    return "my_provider"
```

### `display_name -> str`

Human-readable name for the UI:

```python
@property
def display_name(self) -> str:
    return "My Provider"
```

## Required Methods

### `is_available() -> bool`

Check if the provider can be used:

```python
def is_available(self) -> bool:
    # Public API, always available
    return True
```

### `is_authenticated() -> bool`

```python
def is_authenticated(self) -> bool:
    return bool(self.get_credential("api_key"))
```

### `authenticate() -> bool`

```python
async def authenticate(self) -> bool:
    api_key = self.get_credential("api_key")
    if not api_key:
        raise AuthenticationError("API key not configured")
    # Validate...
    return True
```

### `lookup_by_store_id(store_name, store_id) -> Optional[str]`

This is the primary matching method. Given a store game, find your provider's
internal ID for it:

```python
async def lookup_by_store_id(self, store_name, store_id):
    """Look up by store ID.

    IGDB uses an external_games database for this. Other providers
    might have their own mapping tables.
    """
    # Check local cache first
    cached = self._get_cached_match(store_name, store_id)
    if cached:
        return cached

    # Query the API
    resp = self.http.get(
        "https://api.myprovider.com/v1/match",
        params={"store": store_name, "store_id": store_id},
        timeout=10,
    )
    if resp.status_code == 200:
        data = resp.json()
        if data.get("match"):
            provider_id = str(data["match"]["id"])
            self._cache_match(store_name, store_id, provider_id)
            return provider_id
    return None
```

### `search_game(title, year=None) -> List[MetadataSearchResult]`

Fallback search when `lookup_by_store_id()` returns `None`:

```python
async def search_game(self, title, year=None):
    resp = self.http.get(
        "https://api.myprovider.com/v1/search",
        params={"q": title, "year": year},
        timeout=10,
    )
    resp.raise_for_status()

    results = []
    for item in resp.json().get("results", []):
        results.append(MetadataSearchResult(
            provider_id=str(item["id"]),
            title=item["name"],
            release_year=item.get("year"),
            platforms=item.get("platforms", []),
            cover_url=item.get("cover"),
            confidence=item.get("score", 0.0),
        ))
    return results
```

The default `enrich_games()` implementation uses results with
`confidence >= 0.8`.

### `get_enrichment(provider_id) -> Optional[EnrichmentData]`

Fetch full enrichment data for a matched game:

```python
async def get_enrichment(self, provider_id):
    # Check cache
    cached = self.get_cached_enrichment_by_id(provider_id)
    if cached:
        return cached

    resp = self.http.get(
        f"https://api.myprovider.com/v1/games/{provider_id}",
        timeout=15,
    )
    if resp.status_code != 200:
        return None

    data = resp.json()
    enrichment = EnrichmentData(
        provider_name=self.provider_name,
        provider_id=provider_id,
        genres=data.get("genres", []),
        tags=data.get("tags", []),
        franchise=data.get("franchise"),
        series=data.get("series"),
        developers=data.get("developers", []),
        publishers=data.get("publishers", []),
        summary=data.get("summary"),
        storyline=data.get("storyline"),
        release_date=data.get("release_date"),
        cover_url=data.get("cover_url"),
        screenshots=data.get("screenshots", []),
        user_rating=data.get("user_rating"),
        themes=data.get("themes", []),
        platforms=data.get("platforms", []),
        perspectives=data.get("perspectives", []),
        engine=data.get("engine"),
    )

    # Cache for future use
    self._cache_enrichment(enrichment)
    return enrichment
```

### `get_database_path() -> Path`

```python
def get_database_path(self) -> Path:
    return self.data_dir / "enrichment.db"
```

## The EnrichmentData Dataclass

```python
from luducat.plugins.base import EnrichmentData

enrichment = EnrichmentData(
    provider_name="my_provider",     # Required: your provider name
    provider_id="12345",             # Required: your internal ID

    # Categorization
    genres=["RPG", "Action"],
    tags=["Open World", "Fantasy"],
    franchise="The Elder Scrolls",
    series="Main Series",

    # Additional metadata
    developers=["Bethesda Game Studios"],
    publishers=["Bethesda Softworks"],
    summary="Short summary text",
    storyline="Full story synopsis",
    release_date="2011-11-11",

    # Media
    cover_url="https://...",
    background_url="https://...",
    screenshots=["https://...", "https://..."],

    # Ratings
    user_rating=95.0,
    user_rating_count=50000,

    # Extended
    themes=["Fantasy", "Open World"],
    platforms=["windows", "linux", "ps4"],
    perspectives=["First person", "Third person"],
    age_ratings=[{"rating": "M", "category": "ESRB"}],
    engine="Creation Engine",
    websites=[{"type": "official", "url": "https://..."}],

    # Provider-specific extra data
    extra={"my_score": 98, "my_tags": ["mod-friendly"]},
)
```

All fields except `provider_name` and `provider_id` are optional.

## Optional Methods

### Batch Enrichment

Override `enrich_games()` for batch API optimizations:

```python
async def enrich_games(self, games, status_callback=None,
                        cancel_check=None, cross_store_ids=None):
    """Batch enrichment with a single API call."""
    results = {}

    # Batch lookup
    ids = [g.store_app_id for g in games]
    matches = await self._batch_lookup(ids)

    for i, game in enumerate(games):
        if cancel_check and cancel_check():
            break
        if status_callback:
            status_callback(f"Enriching: {game.title}", i + 1, len(games))

        provider_id = matches.get(game.store_app_id)
        if provider_id:
            enrichment = await self.get_enrichment(provider_id)
            if enrichment:
                results[game.store_app_id] = enrichment

    return results
```

### Cached Enrichment

```python
def get_cached_enrichment(self, store_name, store_id):
    """Return cached enrichment without API calls."""
    provider_id = self._get_cached_match(store_name, store_id)
    if provider_id:
        return self._load_from_db(provider_id)
    return None
```

### Asset Attribution

For plugins that track asset authorship (like SteamGridDB):

```python
def get_asset_attribution(self, asset_url):
    """Return author info for an asset URL."""
    author = self._lookup_author(asset_url)
    if author:
        return {"author": author.name, "author_id": author.id}
    return None

def adjust_author_score(self, author_name, delta):
    """Adjust quality score for an asset author."""
    # Update in settings/database
    return True
```

### Tag Sync

For metadata plugins that import tags from local config files:

```python
# Declare in plugin.json:
# "capabilities": {"tag_sync": true}

def get_tag_sync_data(self):
    """Return tags from local config.

    Returns:
        Dict mapping store identifiers to tag data:
        {
            ("gog", "1234"): {"tags": ["Action", "RPG"]},
            ("epic", "abcd"): {"tags": ["Favorite"], "favorite": True},
        }
    """
    ...
```

## Injected Properties

Same as store plugins:

| Property | Type | Usage |
|----------|------|-------|
| `self.http` | `PluginHttpClient` | `self.http.get(url, timeout=10)` |
| `self.storage` | `PluginStorage` | `self.storage.write_text(...)` |
| `self.config_dir` | `Path` | Plugin config directory |
| `self.cache_dir` | `Path` | Plugin cache directory |
| `self.data_dir` | `Path` | Plugin data directory |

Credential helpers: `self.get_credential(key)`, `self.set_credential(key, value)`,
`self.delete_credential(key)`

Settings: `self.get_setting(key, default=None)`

Privacy: `self.has_local_data_consent()`

## Metadata Priority

Your `provides_fields` in `plugin.json` declares what fields you provide and
at what priority. Lower number = higher priority:

```json
{
  "provides_fields": {
    "genres": {"priority": 20},
    "franchise": {"priority": 10},
    "themes": {"priority": 10}
  }
}
```

If IGDB provides genres at priority 20 and your plugin also provides genres at
priority 15, your data wins. See [plugin.json Reference](plugin-json.md) for
details.

## Checklist

- [ ] All required methods implemented
- [ ] `plugin.json` with `provides_fields` listing your enrichment capabilities
- [ ] `network.allowed_domains` declares API endpoints
- [ ] Enrichment data cached in local database to avoid redundant API calls
- [ ] `lookup_by_store_id()` returns quickly (cache hit) or `None` (cache miss)
- [ ] `search_game()` returns results with reasonable confidence scores
- [ ] Batch operations respect `cancel_check`
- [ ] Tests cover lookup, search, and enrichment paths
