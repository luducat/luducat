# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# vdf_scanner.py

"""Steam VDF file scanner

Parses Steam's local VDF/ACF configuration files to extract:
- User tags/categories from sharedconfig.vdf
- Favorites and hidden games
- Library folder locations from libraryfolders.vdf
- App manifests (installation data) from appmanifest_*.acf

Reuses find_steam_path() and STEAM64_TO_STEAM3_OFFSET from family_sharing.py.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import vdf

from .family_sharing import STEAM64_TO_STEAM3_OFFSET, find_steam_path

logger = logging.getLogger(__name__)


def parse_login_users(steam_path: Path) -> List[Dict[str, Any]]:
    """Parse loginusers.vdf to find Steam accounts.

    Args:
        steam_path: Steam installation directory

    Returns:
        List of dicts with steam_id, persona_name, most_recent (bool)
    """
    path = steam_path / "config" / "loginusers.vdf"
    if not path.exists():
        logger.debug(f"loginusers.vdf not found: {path}")
        return []

    try:
        data = vdf.load(open(path, encoding="utf-8", errors="replace"))
    except Exception as e:
        logger.warning(f"Failed to parse loginusers.vdf: {e}")
        return []

    users_section = data.get("users", {})
    result = []
    for steam_id, info in users_section.items():
        result.append({
            "steam_id": steam_id,
            "persona_name": info.get("PersonaName", ""),
            "most_recent": info.get("MostRecent", "0") == "1",
        })
    return result


def parse_user_config(
    steam_path: Path, steam_id: str
) -> Dict[str, Any]:
    """Parse sharedconfig.vdf to extract user tags, favorites, and hidden games.

    Steam stores user categories/tags under:
    UserRoamingConfigStore.Software.Valve.Steam.apps.<app_id>.tags.<n> = "category name"

    The special category "favorite" (case-insensitive) maps to luducat favorites.
    The field Hidden = "1" maps to luducat hidden status.

    Args:
        steam_path: Steam installation directory
        steam_id: Steam64 ID (17-digit)

    Returns:
        Dict with tags: {app_id: [category_names]}, favorites: {app_ids}, hidden: {app_ids}
    """
    steam3_id = int(steam_id) - STEAM64_TO_STEAM3_OFFSET

    # Try modern path first, then legacy
    candidates = [
        steam_path / "userdata" / str(steam3_id) / "7" / "remote" / "sharedconfig.vdf",
        steam_path / "userdata" / str(steam3_id) / "config" / "sharedconfig.vdf",
    ]

    data = None
    for path in candidates:
        if path.exists():
            try:
                data = vdf.load(open(path, encoding="utf-8", errors="replace"))
                logger.debug(f"Loaded sharedconfig.vdf from: {path}")
                break
            except Exception as e:
                logger.warning(f"Failed to parse {path}: {e}")

    if data is None:
        logger.debug(f"No sharedconfig.vdf found for Steam3 ID {steam3_id}")
        return {"tags": {}, "favorites": set(), "hidden": set()}

    # Navigate to apps section
    # Structure: UserRoamingConfigStore.Software.Valve.Steam.apps
    apps_section = (
        data
        .get("UserRoamingConfigStore", data.get("UserLocalConfigStore", {}))
        .get("Software", {})
        .get("Valve", {})
        .get("Steam", {})
        .get("apps", data
             .get("UserRoamingConfigStore", data.get("UserLocalConfigStore", {}))
             .get("Software", {})
             .get("valve", {})
             .get("steam", {})
             .get("Apps", {}))
    )

    # VDF is case-insensitive, try alternate casing
    if not apps_section:
        apps_section = (
            data
            .get("UserRoamingConfigStore", data.get("UserLocalConfigStore", {}))
            .get("Software", {})
            .get("Valve", {})
            .get("Steam", {})
            .get("Apps", {})
        )

    tags: Dict[str, List[str]] = {}
    favorites: Set[str] = set()
    hidden: Set[str] = set()

    for app_id, app_data in apps_section.items():
        if not isinstance(app_data, dict):
            continue

        # Extract tags/categories
        tag_section = app_data.get("tags", {})
        if isinstance(tag_section, dict):
            category_names = list(tag_section.values())
            if category_names:
                # Check for "favorite" (case-insensitive)
                regular_tags = []
                for cat_name in category_names:
                    if isinstance(cat_name, str):
                        if cat_name.lower() == "favorite":
                            favorites.add(app_id)
                        else:
                            regular_tags.append(cat_name)
                if regular_tags:
                    tags[app_id] = regular_tags

        # Check hidden status
        if app_data.get("Hidden", "0") == "1" or app_data.get("hidden", "0") == "1":
            hidden.add(app_id)

    logger.info(
        f"VDF user config: {len(tags)} games with tags, "
        f"{len(favorites)} favorites, {len(hidden)} hidden"
    )
    return {"tags": tags, "favorites": favorites, "hidden": hidden}


def parse_library_folders(steam_path: Path) -> List[Dict[str, Any]]:
    """Parse libraryfolders.vdf to find all Steam library locations.

    Args:
        steam_path: Steam installation directory

    Returns:
        List of dicts with path, label, totalsize, apps (dict of app_id → size)
    """
    candidates = [
        steam_path / "config" / "libraryfolders.vdf",
        steam_path / "steamapps" / "libraryfolders.vdf",
    ]

    data = None
    for path in candidates:
        if path.exists():
            try:
                data = vdf.load(open(path, encoding="utf-8", errors="replace"))
                logger.debug(f"Loaded libraryfolders.vdf from: {path}")
                break
            except Exception as e:
                logger.warning(f"Failed to parse {path}: {e}")

    if data is None:
        logger.debug("No libraryfolders.vdf found")
        return []

    folders_section = data.get("libraryfolders", data.get("LibraryFolders", {}))
    result = []

    for key, info in folders_section.items():
        if not isinstance(info, dict):
            continue
        folder_path = info.get("path", "")
        if not folder_path:
            continue

        apps = {}
        apps_section = info.get("apps", {})
        if isinstance(apps_section, dict):
            for app_id, size in apps_section.items():
                try:
                    apps[app_id] = int(size) if size else 0
                except (ValueError, TypeError):
                    apps[app_id] = 0

        result.append({
            "path": folder_path,
            "label": info.get("label", ""),
            "totalsize": info.get("totalsize", "0"),
            "apps": apps,
        })

    logger.info(f"Found {len(result)} Steam library folders")
    return result


def parse_app_manifest(manifest_path: Path) -> Optional[Dict[str, Any]]:
    """Parse an appmanifest_*.acf file.

    Args:
        manifest_path: Path to appmanifest_<appid>.acf

    Returns:
        Dict with app_id, name, installdir, size_on_disk, state_flags,
        last_updated, build_id — or None on failure
    """
    if not manifest_path.exists():
        return None

    try:
        data = vdf.load(open(manifest_path, encoding="utf-8", errors="replace"))
    except Exception as e:
        logger.debug(f"Failed to parse {manifest_path}: {e}")
        return None

    app_state = data.get("AppState", {})
    if not app_state:
        return None

    return {
        "app_id": app_state.get("appid", ""),
        "name": app_state.get("name", ""),
        "installdir": app_state.get("installdir", ""),
        "size_on_disk": app_state.get("SizeOnDisk", "0"),
        "state_flags": app_state.get("StateFlags", "0"),
        "last_updated": app_state.get("LastUpdated", "0"),
        "build_id": app_state.get("buildid", ""),
    }


def scan_installed_games(steam_path: Path) -> Dict[str, Dict[str, Any]]:
    """Scan all library folders for installed games.

    Combines parse_library_folders() + parse_app_manifest() across all libraries.

    Args:
        steam_path: Steam installation directory

    Returns:
        Dict mapping app_id → manifest data
    """
    folders = parse_library_folders(steam_path)
    result = {}

    for folder in folders:
        steamapps = Path(folder["path"]) / "steamapps"
        if not steamapps.exists():
            continue

        for manifest_file in steamapps.glob("appmanifest_*.acf"):
            manifest = parse_app_manifest(manifest_file)
            if manifest and manifest.get("app_id"):
                # Add full install path (steamapps/common/{installdir})
                install_dir = manifest.get("installdir", "")
                if install_dir:
                    manifest["full_path"] = str(steamapps / "common" / install_dir)
                result[manifest["app_id"]] = manifest

    logger.info(f"Found {len(result)} installed games across {len(folders)} libraries")
    return result
