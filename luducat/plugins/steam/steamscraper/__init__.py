# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""
Steam Scraper Module

A Python module for scraping and managing Steam game data.

Features:
- SQLite database with schema versioning
- Steam API integration with automatic rate limiting
- Web scraping for screenshots and additional data
- Query by appid or game name with automatic data fetching

Usage:
    from steam_scraper import SteamGameManager

    manager = SteamGameManager()

    # Get a game (auto-fetches if missing)
    game = manager.get_game(appid=440)
    game = manager.get_game(name="Team Fortress 2")

    manager.close()
"""

from .manager import SteamGameManager
from .database import Database, Game, Image
from .steam_api import SteamAPIClient
from .steam_scraper import SteamScraper
from .exceptions import (
    SteamScraperException,
    AppNotFoundError,
    RateLimitExceededError,
    SteamAPIError,
    ScrapingError,
    DatabaseError,
    InvalidDataError
)

__version__ = "1.0.0"
__author__ = "Steam Scraper"

__all__ = [
    'SteamGameManager',
    'Database',
    'Game',
    'Image',
    'SteamAPIClient',
    'SteamScraper',
    'SteamScraperException',
    'AppNotFoundError',
    'RateLimitExceededError',
    'SteamAPIError',
    'ScrapingError',
    'DatabaseError',
    'InvalidDataError',
]
