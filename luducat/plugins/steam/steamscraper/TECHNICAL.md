# Steam Scraper - Technical Documentation

## Architecture Overview

The Steam Scraper module is built with a modular architecture that separates concerns:

```
┌─────────────────┐
│  SteamGameManager│  ← Main API Interface
└────────┬────────┘
         │
    ┌────┴────┬──────────┬─────────────┐
    │         │          │             │
┌───▼───┐ ┌──▼──┐  ┌───▼────┐  ┌─────▼─────┐
│Database│ │Steam│  │Steam   │  │  Kaggle   │
│        │ │API  │  │Scraper │  │  Importer │
└────────┘ └─────┘  └────────┘  └───────────┘
```

## Module Components

### 1. config.py
Configuration constants for the entire module.

**Key Constants:**
- `STEAM_API_KEY`: Your Steam API key
- `CACHE_DIR`: Directory for downloaded images (default: "./cache")
- `DATABASE_PATH`: SQLite database file path
- `CURRENT_SCHEMA_VERSION`: Database schema version (for migrations)
- `MAX_RETRIES`: Number of 429 retries (default: 3)
- `RETRY_WAIT_SECONDS`: Wait time per retry (default: 300)

### 2. exceptions.py
Custom exception hierarchy for error handling.

**Exception Tree:**
```
SteamScraperException (base)
├── AppNotFoundError
├── RateLimitExceededError
├── SteamAPIError
├── ScrapingError
├── DatabaseError
└── InvalidDataError
```

### 3. database.py
SQLAlchemy ORM models and database management.

**Models:**

**Meta Table:**
- Tracks schema version for migrations
- Key-value store for metadata

**Game Table:**
- Primary key: `appid` (integer)
- 40+ fields covering all Steam game data
- JSON columns for arrays (developers, publishers, genres, etc.)
- Relationship to Image table (one-to-many)

**Image Table:**
- Primary key: auto-increment `id`
- Foreign key: `appid` → Game.appid (CASCADE DELETE)
- Fields: filename, image_order, scraped_date

**Database Class:**
- Manages SQLAlchemy engine and sessions
- Handles schema versioning and migrations
- Provides session factory

**Schema Versioning:**
The module tracks schema version in the `meta` table. When `CURRENT_SCHEMA_VERSION` increases, the `_upgrade_schema()` method is called. Migration logic can be added there.

### 4. steam_api.py
Steam API client with automatic rate limit handling.

**Key Features:**

**Automatic 429 Throttling:**
```python
def _make_request(url, params):
    retry_count = 0
    while retry_count <= MAX_RETRIES:
        response = session.get(url, params)
        if response.status_code == 429:
            if retry_count >= MAX_RETRIES:
                raise RateLimitExceededError()
            time.sleep(RETRY_WAIT_SECONDS)
            retry_count += 1
            continue
        return response.json()
```

**API Methods:**
- `get_app_details(appid)`: Fetch complete game data from Steam API
- `get_app_list()`: Get all Steam apps (for search functionality)
- `search_app_by_name(name)`: Find appid by game name

**Rate Limit Behavior:**
1. Make request
2. If 429 → wait 300s, retry
3. If still 429 → wait 300s, retry
4. If still 429 → wait 300s, retry
5. If still 429 → raise exception

Total: Up to 4 attempts (initial + 3 retries)

### 5. steam_scraper.py
Web scraper for Steam store pages.

**Key Features:**

**Screenshot Extraction:**
The scraper parses HTML to find screenshot carousel elements:
```python
screenshot_thumbs = soup.find_all('a', class_='highlight_screenshot_link')
```

Converts thumbnail URLs to full-size:
```
Thumbnail: .116x65.jpg or .600x338.jpg
Full-size: .1920x1080.jpg
```

**Image Download:**
- Creates `cache/{appid}/` directory
- Names images: `{appid}_1.jpg`, `{appid}_2.jpg`, etc.
- Skips if file already exists (idempotent)
- Downloads in chunks (8192 bytes)

**Methods:**
- `get_store_page(appid)`: Fetch HTML from Steam store
- `extract_screenshots(appid)`: Parse HTML and extract screenshot URLs
- `download_image(url, filepath)`: Download single image
- `scrape_screenshots(appid)`: Download all screenshots for a game

### 6. kaggle_importer.py
Imports Kaggle Steam Games Dataset.

**Import Logic:**
1. Load JSON file (expects dict with appid keys)
2. For each entry:
   - Check if appid exists in database
   - If exists → skip
   - If not exists → create Game entry
3. Batch commits every 100 games for performance
4. Returns statistics: total, imported, skipped, failed

**Data Mapping:**
All fields from Kaggle JSON are mapped to the Game model. The importer assumes Kaggle data is complete (`is_complete=True`).

### 7. manager.py
Main orchestration layer - the public API.

**SteamGameManager Class:**

**Initialization:**
```python
def __init__(self, db_path=None, cache_dir=None, api_key=None):
    self.database = Database(db_path)
    self.api_client = SteamAPIClient(api_key)
    self.scraper = SteamScraper(cache_dir)
    self.kaggle_importer = KaggleImporter(self.database)
```

**Primary Method: get_game()**

Flow diagram:
```
get_game(appid=X or name=Y)
    │
    ├─ If name: find appid via _find_appid_by_name()
    │           ├─ Check DB
    │           └─ Query Steam API
    │
    ├─ Query database for Game
    │
    ├─ Check if complete (_is_complete)
    │   Requirements: name AND developers AND publishers
    │
    ├─ If missing/incomplete:
    │   └─ _fetch_and_store_game()
    │       ├─ api_client.get_app_details(appid)
    │       ├─ Create/update Game object
    │       ├─ Mark is_complete
    │       ├─ Commit to database
    │       └─ scrape_and_store_screenshots()
    │
    └─ Return Game object
```

**Data Preservation Logic:**

When updating existing games, the manager preserves data that might be lost if a game is delisted:

```python
# Update only if new data exists
game.description = new_data or game.description
game.website = new_data or game.website
```

This ensures that if Steam removes data (delisted game), we keep what we had.

**Screenshot Management:**

1. Delete existing Image records for appid
2. Download screenshots via scraper
3. Create new Image records
4. Commit to database

### 8. __init__.py
Module exports and public API definition.

Exposes:
- `SteamGameManager` (primary interface)
- Database models (Game, Image, Database)
- Individual components (for advanced usage)
- All exceptions

## Data Flow Examples

### Example 1: First-time Query

```
User: manager.get_game(appid=440)
  │
  └─> SteamGameManager.get_game()
      │
      ├─> Database.query(Game, appid=440) → None
      │
      ├─> _fetch_and_store_game(440)
      │   │
      │   ├─> SteamAPIClient.get_app_details(440)
      │   │   └─> [API call, may retry on 429]
      │   │
      │   ├─> Create Game(appid=440, ...) from API data
      │   │
      │   ├─> Database.add(game) + commit()
      │   │
      │   └─> SteamScraper.scrape_screenshots(440)
      │       ├─> Download images to cache/440/
      │       └─> Create Image records
      │
      └─> Return Game object
```

### Example 2: Existing Complete Game

```
User: manager.get_game(appid=440)
  │
  └─> SteamGameManager.get_game()
      │
      ├─> Database.query(Game, appid=440) → Game found
      │
      ├─> _is_complete(game) → True
      │   (has name, developers, publishers)
      │
      └─> Return Game object (no API call)
```

### Example 3: Incomplete Game (Needs Update)

```
User: manager.get_game(appid=440)
  │
  └─> SteamGameManager.get_game()
      │
      ├─> Database.query(Game, appid=440) → Game found
      │
      ├─> _is_complete(game) → False
      │   (missing developers)
      │
      ├─> _fetch_and_store_game(440, existing_game=game)
      │   │
      │   ├─> SteamAPIClient.get_app_details(440)
      │   │
      │   ├─> _update_game_from_api(game, api_data)
      │   │   └─> Preserves existing data where new is empty
      │   │
      │   └─> Database.commit()
      │
      └─> Return updated Game object
```

## Performance Considerations

### Database
- Uses SQLAlchemy sessions efficiently
- Batch commits during Kaggle import (every 100 games)
- Indexes on primary keys (appid)
- JSON columns for flexible array storage

### API Calls
- No artificial rate limiting (relies on Steam's 429 responses)
- Automatic retry with exponential backoff
- Connection pooling via requests.Session

### Caching
- Images downloaded once and stored locally
- Filename checks prevent re-downloads
- Metadata in database tracks what's been scraped

### Memory
- Sessions closed after use
- Minimal object retention
- Streaming image downloads

## Error Handling Strategy

### Graceful Degradation
- Missing fields don't crash imports
- Partial data better than no data
- Preserves what exists on updates

### Clear Exceptions
- Specific exception types for different failures
- Informative error messages
- Traceable error sources

### Retry Logic
- Only for 429 rate limits
- Fixed retry count (not infinite)
- Clear failure after max retries

## Testing

Run the test suite:
```bash
python test_module.py
```

Tests validate:
- All imports work
- Configuration is valid
- Database creation and schema versioning
- Manager initialization
- Component initialization

## Future Enhancements

Potential improvements:
1. **Concurrent downloads**: Thread pool for image downloads
2. **Progress callbacks**: Monitor import/scrape progress
3. **Incremental updates**: Update only changed games
4. **Advanced search**: Full-text search on descriptions
5. **API fallback**: Try store scraping if API fails
6. **Compression**: Store screenshots in WebP format
7. **Multi-API keys**: Rotate keys to avoid rate limits

## Debugging

Enable detailed logging:
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

This shows:
- All API requests
- Database operations
- Screenshot downloads
- Rate limit events

## Thread Safety

**Not thread-safe** by default. Each thread should use its own:
- Database session
- SteamGameManager instance

For multi-threaded usage:
```python
from threading import local

thread_local = local()

def get_manager():
    if not hasattr(thread_local, 'manager'):
        thread_local.manager = SteamGameManager()
    return thread_local.manager
```

## Memory Management

Best practices:
```python
# Always close when done
manager = SteamGameManager()
try:
    game = manager.get_game(appid=440)
finally:
    manager.close()

# Or use context manager (if implemented)
with SteamGameManager() as manager:
    game = manager.get_game(appid=440)
```

## Database Migrations

When schema changes:
1. Increment `CURRENT_SCHEMA_VERSION` in config.py
2. Add migration logic in `Database._upgrade_schema()`
3. Handle both upgrade and downgrade paths

Example:
```python
def _upgrade_schema(self, session, from_version, to_version):
    if from_version == 1 and to_version == 2:
        # Add new column
        with self.engine.begin() as conn:
            conn.execute("ALTER TABLE games ADD COLUMN new_field TEXT")
```
