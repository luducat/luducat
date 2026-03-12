# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# family_sharing.py

"""Steam Family Sharing support for luducat

Reads family shared games from Steam's local configuration files:
- localconfig.vdf (legacy): WebStorage cache of family member games
- librarycache.vdf (modern): Steam's library database with license types

These games are not accessible via Steam API - they're only stored locally.

The modern librarycache.vdf system was introduced around 2023 for better
sandboxing support (Flatpak, etc). Legacy systems still use localconfig.vdf.
This module supports both for maximum compatibility.
"""

from luducat.plugins.sdk.json import json
import logging
import platform
from pathlib import Path
from typing import List, Optional, Tuple

import vdf

logger = logging.getLogger(__name__)

# Steam ID conversion constant
# Steam stores userdata by Steam3 ID (32-bit), not Steam64 ID (64-bit)
# Steam3 ID = Steam64 ID - 76561197960265728
# The constant 0x0110000100000000 encodes account type (individual) and universe (public)
STEAM64_TO_STEAM3_OFFSET = 76561197960265728


def find_steam_path(custom_path: Optional[str] = None) -> Optional[Path]:
    """Find the Steam installation directory.

    Args:
        custom_path: Optional user-specified path to Steam installation

    Returns:
        Path to Steam directory or None if not found
    """
    if custom_path:
        path = Path(custom_path).expanduser()
        if path.exists():
            return path
        logger.warning(f"Custom Steam path does not exist: {custom_path}")

    system = platform.system()

    if system == "Linux":
        candidates = [
            Path.home() / ".steam" / "steam",
            Path.home() / ".local" / "share" / "Steam",
            Path.home() / ".steam" / "debian-installation",
            Path("/usr/share/steam"),
        ]
    elif system == "Windows":
        candidates = [
            Path("C:/Program Files (x86)/Steam"),
            Path("C:/Program Files/Steam"),
        ]
    elif system == "Darwin":  # macOS
        candidates = [
            Path.home() / "Library" / "Application Support" / "Steam",
        ]
    else:
        candidates = []

    for path in candidates:
        if path.exists():
            logger.debug(f"Found Steam at: {path}")
            return path

    logger.warning("Could not find Steam installation")
    return None


def get_localconfig_path(steam_path: Path, steam_id: str) -> Optional[Path]:
    """Get path to localconfig.vdf for a specific Steam user.

    Args:
        steam_path: Steam installation directory
        steam_id: Steam64 ID (17-digit)

    Returns:
        Path to localconfig.vdf or None if not found
    """
    try:
        steam64 = int(steam_id)
        steam3_id = steam64 - STEAM64_TO_STEAM3_OFFSET
    except (ValueError, TypeError):
        logger.error(f"Invalid Steam ID: {steam_id}")
        return None

    userdata_path = steam_path / "userdata" / str(steam3_id) / "config" / "localconfig.vdf"

    if userdata_path.exists():
        logger.debug(f"Found localconfig.vdf at: {userdata_path}")
        return userdata_path

    # Try searching all userdata folders if exact match not found
    userdata_dir = steam_path / "userdata"
    if userdata_dir.exists():
        for user_dir in userdata_dir.iterdir():
            if user_dir.is_dir():
                config_path = user_dir / "config" / "localconfig.vdf"
                if config_path.exists():
                    logger.info(f"Found localconfig.vdf in alternate location: {config_path}")
                    return config_path

    logger.warning(f"localconfig.vdf not found for Steam ID {steam_id}")
    return None


def get_librarycache_path(steam_path: Path) -> Optional[Path]:
    """Get path to the modern librarycache.vdf file.

    Args:
        steam_path: Steam installation directory

    Returns:
        Path to librarycache.vdf or None if not found
    """
    path = steam_path / "config" / "librarycache.vdf"
    if path.exists():
        logger.debug(f"Found librarycache.vdf at: {path}")
        return path
    logger.debug("librarycache.vdf not found")
    return None


def parse_family_shared_games_legacy(
    localconfig_path: Path,
    owned_app_ids: Optional[set] = None,
) -> Tuple[List[str], Optional[int]]:
    """Parse family shared game IDs from localconfig.vdf (legacy system).

    Family shared games come from FamilyGroup members. We look up each
    family member's games in WebStorage -> FriendsOwnedGames_storage_<steamid>.

    Args:
        localconfig_path: Path to localconfig.vdf
        owned_app_ids: Set of app IDs the user owns (to exclude from results)

    Returns:
        Tuple of (list of borrowed app ID strings, lastFetchTimeMS or None)
    """
    try:
        with open(localconfig_path, "r", encoding="utf-8", errors="replace") as f:
            data = vdf.load(f)
    except Exception as e:
        logger.error(f"Failed to parse localconfig.vdf: {e}")
        return [], None

    try:
        user_config = data.get("UserLocalConfigStore", {})
        family_group = user_config.get("FamilyGroup", {})
        webstorage = user_config.get("WebStorage", {})
    except (AttributeError, TypeError):
        logger.warning("Unexpected localconfig.vdf structure")
        return [], None

    # Get family group member IDs (excluding the current user)
    family_member_ids: set = set()
    members = family_group.get("members", {})
    for member in members.values():
        if isinstance(member, dict):
            account_id = member.get("accountid")
            if account_id:
                family_member_ids.add(str(account_id))

    if not family_member_ids:
        logger.debug("No family group members found in legacy system")
        return [], None

    logger.debug(f"Family group members: {family_member_ids}")

    all_shared_apps: set = set()
    latest_fetch_time: Optional[int] = None

    # Look for games from family members only
    for member_id in family_member_ids:
        key = f"FriendsOwnedGames_storage_{member_id}"
        value = webstorage.get(key)

        if not value:
            logger.debug(f"No cached game data for family member {member_id}")
            continue

        try:
            if isinstance(value, str):
                shared_data = json.loads(value)
            else:
                shared_data = value

            data_obj = shared_data.get("data", {})
            app_ids = data_obj.get("setApps", [])
            fetch_time = data_obj.get("lastFetchTimeMS")

            for app_id in app_ids:
                all_shared_apps.add(str(app_id))

            if fetch_time and (latest_fetch_time is None or fetch_time > latest_fetch_time):
                latest_fetch_time = fetch_time

            logger.debug(f"Found {len(app_ids)} games from family member {member_id}")

        except (json.JSONDecodeError, TypeError, AttributeError) as e:
            logger.warning(f"Failed to parse games for member {member_id}: {e}")
            continue

    # Filter out owned games - only return borrowed games
    if owned_app_ids:
        borrowed_apps = all_shared_apps - owned_app_ids
        logger.info(
            f"Legacy: Found {len(all_shared_apps)} family pool games, "
            f"{len(borrowed_apps)} borrowed (excluding {len(owned_app_ids)} owned)"
        )
        return list(borrowed_apps), latest_fetch_time

    logger.info(f"Legacy: Found {len(all_shared_apps)} total family shared games")
    return list(all_shared_apps), latest_fetch_time


def parse_family_shared_games_modern(
    librarycache_path: Path,
    current_steam_id: Optional[str] = None,
) -> Tuple[List[str], Optional[int]]:
    """Parse family shared game IDs from librarycache.vdf (modern system).

    The modern system stores license type directly per-game. Games with
    licensetype="Borrowed" are family shared.

    Args:
        librarycache_path: Path to librarycache.vdf
        current_steam_id: Current user's Steam64 ID (to skip self-owned games)

    Returns:
        Tuple of (list of borrowed app ID strings, None)
        Note: Modern system does not provide fetch timestamps like legacy system.
    """
    try:
        with open(librarycache_path, "r", encoding="utf-8", errors="replace") as f:
            data = vdf.load(f)
    except Exception as e:
        logger.error(f"Failed to parse librarycache.vdf: {e}")
        return [], None

    borrowed_apps: set = set()
    for app_id, info in data.items():
        if not isinstance(info, dict):
            continue

        licensetype = info.get("licensetype")
        owner = info.get("owner")

        # Only include borrowed games
        if licensetype == "Borrowed":
            # Skip if owner is current user (shouldn't happen, but be safe)
            if current_steam_id and owner == current_steam_id:
                continue
            borrowed_apps.add(str(app_id))

    if borrowed_apps:
        logger.info(f"Modern: Found {len(borrowed_apps)} borrowed games in librarycache")

    return list(borrowed_apps), None


def get_family_shared_games(
    steam_id: str,
    owned_app_ids: Optional[set] = None,
    steam_path: Optional[str] = None,
) -> Tuple[List[str], Optional[int]]:
    """Get list of borrowed game app IDs via family sharing.

    Main entry point for family sharing support. Returns only games that
    are available via family sharing but NOT owned by the user.

    Queries both legacy (localconfig.vdf) and modern (librarycache.vdf)
    systems and combines the results for maximum compatibility.

    Args:
        steam_id: Steam64 ID
        owned_app_ids: Set of app IDs the user owns (to exclude from results)
        steam_path: Optional custom Steam installation path

    Returns:
        Tuple of (list of borrowed app ID strings, lastFetchTimeMS or None)
    """
    path = find_steam_path(steam_path)
    if not path:
        return [], None

    combined_apps: set = set()
    latest_fetch_time: Optional[int] = None

    # Try legacy system first (localconfig.vdf)
    localconfig = get_localconfig_path(path, steam_id)
    if localconfig:
        legacy_apps, legacy_fetch = parse_family_shared_games_legacy(localconfig, owned_app_ids)
        combined_apps.update(legacy_apps)
        if legacy_fetch and (latest_fetch_time is None or legacy_fetch > latest_fetch_time):
            latest_fetch_time = legacy_fetch

    # Try modern system (librarycache.vdf)
    librarycache = get_librarycache_path(path)
    if librarycache:
        modern_apps, _ = parse_family_shared_games_modern(librarycache, current_steam_id=steam_id)
        # Filter out owned apps for modern system too
        if owned_app_ids:
            modern_apps = [a for a in modern_apps if a not in owned_app_ids]
        combined_apps.update(modern_apps)

    if combined_apps:
        logger.info(f"Total family shared games: {len(combined_apps)}")

    return list(combined_apps), latest_fetch_time
