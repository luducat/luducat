# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# config.py

"""
Configuration constants for Steam Scraper module.
"""

# Steam API Key - must be provided via SteamGameManager constructor
# Get your key from https://steamcommunity.com/dev/apikey
STEAM_API_KEY = None

# Cache directory for downloaded images
CACHE_DIR = "./cache"

# Database file path
DATABASE_PATH = "./steam_games.db"

# Schema version for migrations
CURRENT_SCHEMA_VERSION = 7

# SteamSpy fallback threshold
# If SteamSpy returns fewer than this many total reviews (positive + negative),
# fall back to scraping the store page for more accurate counts.
# Set to 0 to always use SteamSpy data without fallback.
STEAMSPY_MIN_REVIEWS = 128

# Steam API endpoints
STEAM_API_BASE = "https://api.steampowered.com"
STEAM_STORE_API = "https://store.steampowered.com/api"
STEAM_STORE_BASE = "https://store.steampowered.com"

# Rate limiting
MAX_RETRIES = 3
RETRY_WAIT_SECONDS = 300
PROACTIVE_COOLDOWN_REQUESTS = 1000   # Pause every N requests to Steam
PROACTIVE_COOLDOWN_SECONDS = 300     # 5 min proactive pause
PROACTIVE_BUDGET_THRESHOLD = 800     # Trigger enrichment interleave at 80% budget
FORBIDDEN_WAIT_SECONDS = 900         # 15 min backoff on 403

# Request timeout
REQUEST_TIMEOUT = 15

# HEAD probe timeout (asset URL validation — no data transfer)
PROBE_TIMEOUT = 5
