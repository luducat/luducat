# When Are Images Downloaded?

## TL;DR

**Images are downloaded in two scenarios:**

1. ✅ **First time fetching a game** - `get_game(appid)` when game doesn't exist
2. ✅ **Manually refreshing** - `refresh_images(appid)` or CLI `refresh` command
3. ❌ **NOT when loading existing complete games** from database

## Detailed Flow

### Scenario 1: First Time Fetching (Automatic)

When you call `get_game()` for a game that doesn't exist or is incomplete:

```python
manager = SteamGameManager()
game = manager.get_game(appid=440)  # Game not in database
```

**What happens:**
```
1. Check database → Game not found or incomplete
2. Call Steam API → Get game data + image URLs
3. Store game data in database
4. ⬇️ DOWNLOAD SCREENSHOTS ⬇️
   └─> cache/440/440_1.jpg, 440_2.jpg, ...
5. ⬇️ DOWNLOAD ADDITIONAL IMAGES ⬇️
   └─> cache/440/header.jpg, background.jpg, logo.png
6. Update database with image metadata
7. Return game object
```

**Images downloaded:**
- ✅ All screenshots from API
- ✅ Header/capsule image
- ✅ Background image  
- ✅ Logo

### Scenario 2: Loading Existing Complete Game (NO Download)

When you call `get_game()` for a game that already exists and is complete:

```python
manager = SteamGameManager()
game = manager.get_game(appid=440)  # Game exists, complete
```

**What happens:**
```
1. Check database → Game found and complete
2. Load from database (with image metadata)
3. ❌ NO API CALL
4. ❌ NO IMAGE DOWNLOADS
5. Return game object
```

**Result:** 
- Game data returned instantly from database
- Image files already in cache (from previous download)
- No network activity

### Scenario 3: Incomplete Game (Download)

When a game exists but is missing required fields (name, developer, or publisher):

```python
# Game in DB but incomplete (e.g., from partial Kaggle import)
game = manager.get_game(appid=999)
```

**What happens:**
```
1. Check database → Game found but INCOMPLETE
2. Call Steam API → Get fresh data
3. Update game in database
4. ⬇️ DOWNLOAD SCREENSHOTS ⬇️
5. ⬇️ DOWNLOAD ADDITIONAL IMAGES ⬇️
6. Return updated game object
```

### Scenario 4: Manual Refresh (Download)

When you explicitly refresh images:

```python
# Python API
stats = manager.refresh_images(appid=440)

# Or CLI
# python cli.py refresh --appid 440
```

**What happens:**
```
1. Load game from database
2. Call Steam API → Get latest image URLs
3. For each image:
   - Check if file exists in cache
   - Check if file is older than 7 days
   - ⬇️ DOWNLOAD if missing or stale
   - Skip if up-to-date
4. Update image metadata in database
5. Return statistics
```

**Smart refresh logic:**
- Downloads missing images
- Updates images older than 7 days
- Skips fresh images (< 7 days)

### Scenario 5: Kaggle Import (NO Download)

When importing from Kaggle dataset:

```python
manager.import_kaggle_dataset("games.json")
```

**What happens:**
```
1. Load JSON data
2. For each game:
   - Check if appid exists in DB
   - If NEW → Create game record
   - ❌ NO IMAGE DOWNLOADS
3. Return import statistics
```

**Note:** Kaggle import only adds game data, not images. To download images after Kaggle import:

```bash
python cli.py refresh --all
```

## Image Download Timing Summary

| Action | Database Check | API Call | Download Images? |
|--------|---------------|----------|------------------|
| `get_game()` - new game | ✅ Not found | ✅ Yes | ✅ **YES** |
| `get_game()` - incomplete | ✅ Found | ✅ Yes | ✅ **YES** |
| `get_game()` - complete | ✅ Found | ❌ No | ❌ **NO** |
| `refresh_images()` | ✅ Found | ✅ Yes | ✅ **YES** (smart) |
| `import_kaggle_dataset()` | N/A | ❌ No | ❌ **NO** |

## Code Example: Tracking Downloads

Here's how to see when downloads happen:

```python
from steam_scraper import SteamGameManager
import logging

# Enable debug logging
logging.basicConfig(level=logging.INFO)

manager = SteamGameManager()

# First call - will download
print("=== First call ===")
game1 = manager.get_game(appid=440)
# Log output: "Game 440 missing or incomplete, fetching from Steam..."
# Log output: "Fetching app details for appid: 440"
# Log output: "Downloaded 10 screenshots for app 440"
# Log output: "Downloaded 4 additional images for app 440"

# Second call - NO download
print("\n=== Second call ===")
game2 = manager.get_game(appid=440)
# No "fetching" log - loaded from database
# No download logs

# Manual refresh - will download based on staleness
print("\n=== Manual refresh ===")
stats = manager.refresh_images(appid=440)
# Log output: "Refreshing images for 440..."
# Downloads only if files are missing or >7 days old

manager.close()
```

## Controlling When Downloads Happen

### Prevent Downloads (Use Database Only)

If you want to only use database data without triggering downloads:

```python
from steam_scraper.database import Game

manager = SteamGameManager()
session = manager.database.get_session()

# Direct database query - NO auto-download
game = session.query(Game).filter_by(appid=440).first()

if game:
    print(f"Name: {game.name}")
    # Note: game.images will fail if detached - use within session
else:
    print("Game not in database")

session.close()
manager.close()
```

### Force Downloads (Even for Complete Games)

If you want to re-download everything:

```python
# Use refresh to re-download images
stats = manager.refresh_images(appid=440)

# This checks staleness and downloads as needed
```

## Workflow Recommendations

### Initial Setup (Bulk Download)

```python
# 1. Import Kaggle dataset (fast, no downloads)
manager.import_kaggle_dataset("games.json")
# Database now has thousands of games

# 2. Refresh images for games you need
priority_games = [440, 570, 730, 271590]  # Popular games
for appid in priority_games:
    manager.refresh_images(appid)

# Or refresh all (slow!)
# manager.refresh_all_images(limit=1000)
```

### Daily Usage

```python
# Normal queries use database (fast)
game = manager.get_game(appid=440)  # Instant if exists

# Only refresh periodically
# Cron job: python cli.py refresh --all --limit 100
```

### On-Demand

```python
# Get game (auto-downloads if missing)
game = manager.get_game(appid=999999)

# If game exists but you want fresh images
stats = manager.refresh_images(appid=999999)
```

## Cache Management

### Check What's Downloaded

```bash
# See cache size
du -sh cache/

# See specific game
ls -lh cache/440/
# header.jpg, background.jpg, logo.png, 440_1.jpg, ...
```

### Re-download Missing Images

```bash
# If cache directory is deleted or corrupted
python cli.py refresh --all

# Will re-download all images based on database URLs
```

## Performance Notes

### First Download
- API call: ~1-2 seconds
- Screenshot downloads: ~5-10 seconds (10 images @ 1MB each)
- Additional images: ~2-3 seconds (4 images @ 500KB each)
- **Total: ~8-15 seconds per game**

### Subsequent Loads
- Database query: ~10-50ms
- No downloads
- **Total: <100ms per game**

### Refresh (Smart)
- API call: ~1-2 seconds
- Downloads only stale/missing images
- Most images skipped if recent
- **Total: ~2-5 seconds per game**

## FAQ

**Q: Do images download when I import Kaggle data?**
A: No. Kaggle import only adds database records. Use refresh to download images.

**Q: Will calling get_game() repeatedly download images each time?**
A: No. First call downloads, subsequent calls use database (no download).

**Q: How do I force re-download all images?**
A: Use `refresh_images(appid)` or delete the cache directory and refresh.

**Q: Can I download only screenshots, not header/logo?**
A: Not currently. The module downloads all image types together.

**Q: Do images download in the background?**
A: No. Downloads are synchronous - `get_game()` blocks until complete.

**Q: What if I only want to check if a game exists?**
A: Query the database directly instead of using `get_game()` to avoid auto-download.
