# Steam Scraper - Quick Start Guide

## Installation (5 minutes)

### Step 1: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 2: Configure API Key
Edit `steam_scraper/config.py`:
```python
STEAM_API_KEY = "YOUR_ACTUAL_API_KEY_HERE"
```

Get your key from: https://steamcommunity.com/dev/apikey

### Step 3: Test Installation
```bash
python test_module.py
```

You should see: ✓ All tests passed!

## Basic Usage (2 minutes)

### Example 1: Get a Game
```python
from steam_scraper import SteamGameManager

# Initialize
manager = SteamGameManager()

# Get Team Fortress 2
game = manager.get_game(appid=440)

print(game.name)           # "Team Fortress 2"
print(game.developers)     # ["Valve"]
print(len(game.images))    # Number of screenshots

# Clean up
manager.close()
```

### Example 2: Search by Name
```python
from steam_scraper import SteamGameManager

manager = SteamGameManager()

# Search for Portal
game = manager.get_game(name="Portal 2")

print(f"Found: {game.name} (appid: {game.appid})")

manager.close()
```

### Example 3: Import Kaggle Dataset
```python
from steam_scraper import SteamGameManager

manager = SteamGameManager()

# Import dataset
stats = manager.import_kaggle_dataset("steam_games.json")

print(f"Imported: {stats['imported']} games")
print(f"Skipped: {stats['skipped']} (already in DB)")

manager.close()
```

## CLI Usage

### Get Game Info
```bash
# By appid
python cli.py get --appid 440

# By name
python cli.py get --name "Half-Life"

# JSON output
python cli.py get --appid 440 --json
```

### Import Dataset
```bash
python cli.py import steam_games.json
```

### Search Database
```bash
python cli.py search "Portal"
```

### Refresh Image Cache
```bash
# Refresh specific game
python cli.py refresh --appid 440

# Refresh all games
python cli.py refresh --all

# Refresh with limit
python cli.py refresh --all --limit 50
```

## Common Tasks

### Check What's in Database
```python
from steam_scraper import SteamGameManager
from steam_scraper.database import Game

manager = SteamGameManager()
session = manager.database.get_session()

# Count games
total = session.query(Game).count()
print(f"Total games: {total}")

# Find incomplete games
incomplete = session.query(Game).filter_by(is_complete=False).all()
print(f"Incomplete: {len(incomplete)}")

session.close()
manager.close()
```

### Download Screenshots for Multiple Games
```python
from steam_scraper import SteamGameManager

manager = SteamGameManager()

# List of popular games
appids = [440, 570, 730, 570940, 271590]

for appid in appids:
    game = manager.get_game(appid=appid)
    print(f"{game.name}: {len(game.images)} screenshots downloaded")

manager.close()
```

### Handle Errors
```python
from steam_scraper import SteamGameManager, AppNotFoundError

manager = SteamGameManager()

try:
    game = manager.get_game(appid=999999999)
except AppNotFoundError:
    print("Game not found!")

manager.close()
```

## File Locations

After running:
- **Database:** `./steam_games.db`
- **Screenshots:** `./cache/{appid}/{appid}_1.jpg, {appid}_2.jpg, ...`

## What Happens Automatically

1. **First Query:** Fetches from Steam API + downloads screenshots
2. **Second Query:** Uses local database (instant)
3. **Incomplete Data:** Auto-fetches from Steam
4. **Rate Limiting:** Waits 300s on 429, up to 3 retries
5. **Image Caching:** Skips downloads if files exist

## Tips

- **Start Small:** Test with 1-2 games before bulk operations
- **Monitor Cache:** Screenshots accumulate; clean periodically
- **Check Logs:** Enable logging to see what's happening
- **Be Patient:** First queries take longer (API + downloads)

## Enable Logging
```python
import logging
logging.basicConfig(level=logging.INFO)

from steam_scraper import SteamGameManager
# Now you'll see detailed logs
```

## Next Steps

- Read **README.md** for full documentation
- Read **TECHNICAL.md** for architecture details
- Run **example.py** for more examples
- Check **test_module.py** for module tests

## Troubleshooting

**"No module named 'sqlalchemy'"**
→ Run: `pip install -r requirements.txt`

**"App not found"**
→ Check appid is valid on Steam

**"Rate limit exceeded"**
→ Module already waited 15 minutes (3×300s), try again later

**No screenshots downloaded**
→ Check `cache/{appid}/` directory exists and has write permissions

## Support

For questions or issues, check:
1. **README.md** - User documentation
2. **TECHNICAL.md** - Developer documentation
3. Module docstrings in code

Happy scraping! 🎮
