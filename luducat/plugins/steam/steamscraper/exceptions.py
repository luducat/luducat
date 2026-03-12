# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# exceptions.py

"""
Custom exceptions for Steam Scraper module.
"""


class SteamScraperException(Exception):
    """Base exception for all steam scraper errors."""
    pass


class AppNotFoundError(SteamScraperException):
    """Raised when a Steam app/game is not found."""
    pass


class RateLimitExceededError(SteamScraperException):
    """Raised when rate limit is exceeded (429/403 response or proactive cooldown)."""
    def __init__(self, message: str = "Rate limit exceeded", wait_seconds: int = 300, reason: str = "429"):
        super().__init__(message)
        self.wait_seconds = wait_seconds
        self.reason = reason  # "429", "403", "proactive"


class SteamAPIError(SteamScraperException):
    """Raised when Steam API returns an error."""
    pass


class ScrapingError(SteamScraperException):
    """Raised when web scraping fails."""
    pass


class DatabaseError(SteamScraperException):
    """Raised when database operations fail."""
    pass


class InvalidDataError(SteamScraperException):
    """Raised when data validation fails."""
    pass
