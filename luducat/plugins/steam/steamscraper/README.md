# Steam Scraper Module

A Python module for scraping and managing Steam game data with automatic rate limiting, database storage, and Kaggle dataset import support.

## Features

- **SQLite Database**: Schema versioning with SQLAlchemy ORM
- **Steam API Integration**: Automatic 429 rate limit handling (3 retries × 300s wait)
- **Web Scraping**: High-resolution screenshot downloading
- **Smart Data Management**: Only fetches missing/incomplete data
- **Kaggle Import**: Bulk import from Kaggle Steam Games Dataset
- **Query Interface**: Search by appid or game name
- **Data Preservation**: Protects existing data when games are delisted

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Before using the module, update your Steam API key in `steam_scraper/config.py`:

```python
STEAM_API_KEY = "YOUR_STEAM_API_KEY_HERE"
```

Get your Steam API key from: https://steamcommunity.com/dev/apikey

## Module Usage

### Basic Usage

```python
from steam_scraper import SteamGameManager

# Initialize manager
manager = SteamGameManager()

# Get a game by appid (auto-fetches if not in database)
game = manager.get_game(appid=440)
print(f"Game: {game.name}")
print(f"Developers: {game.developers}")
print(f"Price: ${game.price}")

# Get a game by name
game = manager.get_game(name="Team Fortress 2")

# Always close when done
manager.close()
```

### Kaggle Dataset Import

```python
from steam_scraper import SteamGameManager

manager = SteamGameManager()

# Import Kaggle dataset (only imports new appids)
stats = manager.import_kaggle_dataset("path/to/games.json")

print(f"Imported: {stats['imported']}")
print(f"Skipped: {stats['skipped']}")
print(f"Failed: {stats['failed']}")

manager.close()
```

### Advanced Usage

```python
from steam_scraper import SteamGameManager, AppNotFoundError

manager = SteamGameManager(
    db_path="./my_games.db",        # Custom database path
    cache_dir="./my_cache",         # Custom cache directory
    api_key="YOUR_CUSTOM_API_KEY"   # Custom API key
)

try:
    # Query game
    game = manager.get_game(appid=12345)
    
    # Check completeness
    if game.is_complete:
        print("Game data is complete")
    
    # Access images
    for image in game.images:
        print(f"Image: {image.filename}")
        
except AppNotFoundError:
    print("Game not found on Steam")
finally:
    manager.close()
```

## CLI Usage

The module includes a CLI tool for testing and manual operations.

### Get Game Information

```bash
# By appid
python cli.py get --appid 440

# By name
python cli.py get --name "Team Fortress 2"

# Output as JSON
python cli.py get --appid 440 --json
```

### Import Kaggle Dataset

```bash
python cli.py import games.json
```

### Import Steam Userdata (Bulk Load Your Library)

```bash
# Download userdata JSON from: https://store.steampowered.com/dynamicstore/userdata/
# (must be logged into Steam in browser)
python cli.py import-userdata userdata.json

# Only check database, don't fetch missing games
python cli.py import-userdata userdata.json --no-fetch
```

**Why use userdata import?**
- ✅ Includes ALL owned apps (games + DLC)
- ✅ Includes profile-limited games
- ✅ Perfect for bulk loading entire library (3000+ games in one command)
- ✅ More complete than GetOwnedGames API

See `USERDATA_GUIDE.md` for detailed instructions.

### Search Database

```bash
# Search for games
python cli.py search "Half-Life"

# Limit results
python cli.py search "Portal" --limit 5
```

### Refresh Image Cache

```bash
# Refresh images for a specific game
python cli.py refresh --appid 440

# Refresh images for all games in database
python cli.py refresh --all

# Refresh images for first 100 games (useful for large databases)
python cli.py refresh --all --limit 100
```

**Refresh behavior:**
- Downloads images if they don't exist in the cache
- Re-downloads images if they're older than 7 days
- Skips images that are up-to-date

## Database Schema

### Games Table

Stores comprehensive game information including:
- Basic info: name, release_date, price, required_age
- Descriptions: detailed_description, about_the_game, short_description
- Platform support: windows, mac, linux
- Metadata: developers, publishers, genres, categories, tags
- Stats: achievements, recommendations, user scores
- Ownership/playtime data

### Images Table

Stores screenshot metadata:
- appid (foreign key)
- filename (e.g., "440_1.jpg")
- image_order
- scraped_date

### Meta Table

Tracks schema version for migrations.

## Data Flow

1. **Query**: `manager.get_game(appid=X)`
2. **Check Database**: Does game exist?
3. **Check Completeness**: Has name, developer, publisher?
4. **If Missing/Incomplete**:
   - Fetch from Steam API
   - Download screenshots
   - Store in database
5. **Return**: Game object

## Rate Limiting

The module handles Steam's rate limiting automatically:
- No artificial delays between requests
- On HTTP 429: waits 300 seconds, retries
- Maximum 3 retries (4 total attempts)
- After 3 retries: raises `RateLimitExceededError`

## Image Handling

Screenshots are:
- Downloaded at highest resolution (1920x1080)
- Stored in `cache/{appid}/` directory
- Named as `{appid}_1.jpg`, `{appid}_2.jpg`, etc.
- Only downloaded once (skips if file exists)
- Metadata stored in database

## Data Preservation

When updating existing games:
- Preserves fields if new data is empty (delisted games)
- Example: If description exists locally but API returns empty, keeps local version
- Ensures data isn't lost when games are removed from Steam

## Error Handling

The module raises specific exceptions:

```python
from steam_scraper.exceptions import (
    AppNotFoundError,           # Game doesn't exist
    RateLimitExceededError,     # Too many 429 responses
    SteamAPIError,              # API request failed
    ScrapingError,              # Web scraping failed
    DatabaseError,              # Database operation failed
    InvalidDataError            # Invalid data format
)
```

## Example: Complete Workflow

```python
from steam_scraper import SteamGameManager

manager = SteamGameManager()

# Import bulk data from Kaggle
print("Importing Kaggle dataset...")
stats = manager.import_kaggle_dataset("steam_games.json")
print(f"Imported {stats['imported']} games")

# Query specific games (will fetch from Steam if not in Kaggle dataset)
games_to_check = [440, 570, 730]  # TF2, Dota 2, CS:GO

for appid in games_to_check:
    game = manager.get_game(appid=appid)
    print(f"{game.name}: {len(game.images)} screenshots")

# Search for a game by name
game = manager.get_game(name="Portal 2")
print(f"Found: {game.name} (appid: {game.appid})")

manager.close()
```

## File Structure

```
steam_scraper/
├── __init__.py          # Module exports
├── config.py            # Configuration constants
├── database.py          # SQLAlchemy models & migrations
├── exceptions.py        # Custom exceptions
├── kaggle_importer.py   # Kaggle dataset import
├── manager.py           # Main orchestration
├── steam_api.py         # Steam API client
└── steam_scraper.py     # Web scraping

cli.py                   # CLI application
requirements.txt         # Dependencies
README.md               # This file
```

## Kaggle Dataset Format

The module expects the Kaggle dataset as a JSON file with this structure:

```json
{
  "20200": {
    "name": "Game Name",
    "developers": ["Developer Name"],
    "publishers": ["Publisher Name"],
    "release_date": "Oct 21, 2008",
    "price": 19.99,
    "screenshots": [
      "https://cdn.akamai.steamstatic.com/steam/apps/20200/screenshot1.jpg"
    ],
    ...
  },
  "440": { ... }
}
```

## Notes

- First run creates `steam_games.db` in current directory
- Cache folder `./cache` created automatically
- Schema version tracked for future migrations
- All timestamps in UTC
- Screenshots exclude videos (only images)

## License

This is a personal utility module. Use responsibly and respect Steam's Terms of Service.

## Requirements

- Python 3.7+
- SQLAlchemy 2.0+
- requests 2.31+
- beautifulsoup4 4.12+
- lxml 4.9+
