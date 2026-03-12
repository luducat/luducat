# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# config.example.py

"""
Example configuration for Steam Scraper.

Copy this file to config.py and customize as needed.
"""

# Steam API Key - Get from https://steamcommunity.com/dev/apikey
STEAM_API_KEY = "YOUR_STEAM_API_KEY_HERE"

# Cache directory for downloaded images
CACHE_DIR = "./cache"

# Database file path
DATABASE_PATH = "./steam_games.db"

# Schema version for migrations (do not change)
CURRENT_SCHEMA_VERSION = 5

# ============================================================================
# STEAMSPY FALLBACK CONFIGURATION
# ============================================================================
# When SteamSpy returns fewer than this many total reviews (positive + negative),
# the scraper will fall back to scraping the Steam store page for more accurate counts.
#
# Recommended values:
#   20  - Default, good balance (use store page for very low review counts)
#   0   - Always use SteamSpy, never use store page fallback
#   50  - More aggressive fallback, better for indie/new games
#   100 - Very aggressive, only trust SteamSpy with 100+ reviews
#
# Why this matters:
#   SteamSpy can be unreliable for games with very few reviews (<20).
#   Store page scraping provides exact counts but is slower and more fragile.
#
STEAMSPY_MIN_REVIEWS = 20

# ============================================================================
# API ENDPOINTS (do not change unless Steam changes their API)
# ============================================================================
STEAM_API_BASE = "https://api.steampowered.com"
STEAM_STORE_API = "https://store.steampowered.com/api"
STEAM_STORE_BASE = "https://store.steampowered.com"

# Rate limiting
MAX_RETRIES = 3
RETRY_WAIT_SECONDS = 300  # 5 minutes

# Request timeout (seconds)
REQUEST_TIMEOUT = 30
