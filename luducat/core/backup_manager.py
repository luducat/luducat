# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# backup_manager.py

"""Backup manager for luducat.

Handles backup creation, restoration, and retention policy.
Used by both the settings dialog and startup checks.
"""

from luducat.core.json_compat import json
import hashlib
import logging
import sqlite3
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .config import Config, get_cache_dir, get_config_dir, get_data_dir
from .constants import APP_VERSION, DEFAULT_BACKUP_DIRNAME
from .directory_health import check_directory

logger = logging.getLogger(__name__)

# Backups older than this version cannot be restored (schema too old).
MIN_RESTORE_VERSION = "0.2.9.33"


def get_default_backup_dir() -> Path:
    """Get the default backup directory on user's Desktop."""
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        return desktop / DEFAULT_BACKUP_DIRNAME
    return Path.home() / DEFAULT_BACKUP_DIRNAME


def get_backup_dir(config: Config) -> Path:
    """Get the configured or default backup directory."""
    custom_path = config.get("backup.location", "").strip()
    if custom_path:
        return Path(custom_path)
    return get_default_backup_dir()


def is_backup_due(config: Config) -> bool:
    """Check if a scheduled backup is due.

    Returns:
        True if backup is enabled and due based on schedule.
    """
    if not config.get("backup.schedule_enabled", False):
        return False

    if not config.get("backup.check_on_startup", True):
        return False

    last_backup_str = config.get("backup.last_backup", "")
    if not last_backup_str:
        # Never backed up, so it's due
        return True

    try:
        last_backup = datetime.fromisoformat(last_backup_str)
        interval_days = config.get("backup.interval_days", 1)
        next_due = last_backup + timedelta(days=interval_days)
        return datetime.now() >= next_due
    except ValueError:
        # Invalid date, assume due
        return True


def _should_exclude(path: Path) -> bool:
    """Check if a file/directory should be excluded from backup.

    Excludes:
        - __pycache__ directories
        - *.pyc files
    """
    # Check if any parent is __pycache__
    for part in path.parts:
        if part == "__pycache__":
            return True
    # Check file extension
    if path.suffix == ".pyc":
        return True
    return False


def collect_backup_items() -> List[Tuple[str, Path]]:
    """Collect core data files to include in backup.

    Includes:
        - config.toml (main configuration)
        - games.db (main database with tags, favorites, etc.)
        - trust-state.json (plugin trust data)
        - Plugin code from config_dir/plugins/ (excluding __pycache__ and *.pyc)
        - Plugin data from data_dir/plugins-data/ (including catalog databases)
        - Custom themes (*.qss files)

    Returns:
        List of (archive_name, source_path) tuples.
    """
    config_dir = get_config_dir()
    data_dir = get_data_dir()

    backup_items = []

    # Config file
    config_file = config_dir / "config.toml"
    if config_file.exists():
        backup_items.append(("config.toml", config_file))

    # Main database (contains tags, favorites, game metadata)
    db_file = data_dir / "games.db"
    if db_file.exists():
        backup_items.append(("games.db", db_file))

    # Trust state (plugin integrity data)
    trust_file = data_dir / "trust-state.json"
    if trust_file.exists():
        backup_items.append(("trust-state.json", trust_file))

    # Plugin code and settings from config_dir/plugins/
    plugins_code_dir = config_dir / "plugins"
    if plugins_code_dir.exists():
        for file_path in plugins_code_dir.rglob("*"):
            if file_path.is_file() and not _should_exclude(file_path):
                rel_path = file_path.relative_to(plugins_code_dir)
                backup_items.append((f"plugins/{rel_path}", file_path))

    # Plugin data (databases, etc.) from data_dir/plugins-data/
    plugins_data_dir = data_dir / "plugins-data"
    if plugins_data_dir.exists():
        for file_path in plugins_data_dir.rglob("*"):
            if file_path.is_file() and not _should_exclude(file_path):
                rel_path = file_path.relative_to(plugins_data_dir)
                backup_items.append((f"plugins-data/{rel_path}", file_path))

    # Custom themes (*.qss files)
    themes_dir = config_dir / "themes"
    if themes_dir.exists():
        for qss_file in themes_dir.glob("*.qss"):
            backup_items.append((f"themes/{qss_file.name}", qss_file))

    return backup_items


def collect_assets_items(config: Config) -> List[Tuple[str, Path]]:
    """Collect image cache files for the assets backup.

    Only includes directories whose config toggle is enabled:
        - backup.include_covers  -> covers/
        - backup.include_heroes  -> heroes/ + description_images/
        - backup.include_screenshots -> screenshots/

    Returns:
        List of (archive_name, source_path) tuples.
    """
    cache_dir = get_cache_dir()
    if not cache_dir.exists():
        return []

    items = []
    dir_map = {
        "backup.include_covers": ["covers"],
        "backup.include_heroes": ["heroes", "description_images"],
        "backup.include_screenshots": ["screenshots"],
    }
    for key, dirs in dir_map.items():
        if not config.get(key, False):
            continue
        for dirname in dirs:
            d = cache_dir / dirname
            if not d.exists():
                continue
            for fp in d.rglob("*"):
                if fp.is_file():
                    rel = fp.relative_to(cache_dir)
                    items.append((str(rel), fp))
    return items


def get_cache_dir_sizes() -> Dict[str, int]:
    """Compute byte sizes for each image cache subdirectory.

    Returns:
        Dict with keys 'covers', 'heroes', 'screenshots' -> size in bytes.
    """
    cache_dir = get_cache_dir()
    result = {"covers": 0, "heroes": 0, "screenshots": 0}
    dir_map = {
        "covers": ["covers"],
        "heroes": ["heroes", "description_images"],
        "screenshots": ["screenshots"],
    }
    for key, dirs in dir_map.items():
        total = 0
        for dirname in dirs:
            d = cache_dir / dirname
            if d.exists():
                for fp in d.rglob("*"):
                    if fp.is_file():
                        try:
                            total += fp.stat().st_size
                        except OSError:
                            pass
        result[key] = total
    return result


def create_backup(
    config: Config,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    file_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str, Optional[str]]:
    """Create a backup (data ZIP, optionally an assets ZIP).

    Args:
        config: Application config
        progress_callback: Optional callback(message, current, total) for progress
        file_callback: Optional callback(path_str) called when a new output file
            starts being written (data ZIP, then assets ZIP).

    Returns:
        Tuple of (success, data_path_or_error, assets_path_or_none)
    """
    backup_dir = get_backup_dir(config)
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Pre-flight: check backup directory health
    health = check_directory(backup_dir)
    if not health.writable:
        return False, f"Backup directory not writable: {health.error or backup_dir}", None

    # Flush WAL to ensure games.db is fully up to date before copying
    db_path = get_data_dir() / "games.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception as e:
            logger.warning(f"WAL checkpoint before backup failed: {e}")

    backup_items = collect_backup_items()

    if not backup_items:
        return False, "No configuration or data files found to backup.", None

    assets_items = collect_assets_items(config)

    # Check free space vs estimated backup size
    all_items = backup_items + assets_items
    estimated_bytes = sum(p.stat().st_size for _, p in all_items if p.is_file())
    required_mb = max(1, int(estimated_bytes / (1024 * 1024)))
    if health.free_mb < required_mb:
        return False, (
            f"Insufficient disk space for backup: "
            f"need ~{required_mb} MB, have {health.free_mb} MB free"
        ), None

    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"luducat_backup_{timestamp}.zip"
    assets_file = backup_dir / f"luducat_assets_{timestamp}.zip" if assets_items else None

    try:
        total_items = len(backup_items) + len(assets_items) + 1  # +1 for metadata
        current = 0

        if file_callback:
            file_callback(str(backup_file))
        if progress_callback:
            progress_callback("Creating backup...", current, total_items)

        with zipfile.ZipFile(backup_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Build file manifest with SHA256 checksums
            file_checksums = {}

            if progress_callback:
                progress_callback("Writing metadata...", current, total_items)

            # Add all files, computing checksums as we go
            for archive_name, source_path in backup_items:
                data = source_path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                file_checksums[archive_name] = {
                    "sha256": digest,
                    "size": len(data),
                }
                zf.writestr(archive_name, data)
                current += 1

                if progress_callback:
                    progress_callback(f"Backing up {archive_name}...", current, total_items)

            # Write metadata last (so checksums are complete)
            backup_info = {
                "version": APP_VERSION,
                "created": datetime.now().isoformat(),
                "files": file_checksums,
                "assets_file": assets_file.name if assets_file else None,
            }
            zf.writestr("backup_info.json", json.dumps(backup_info, indent=2))
            current += 1

            if progress_callback:
                progress_callback("Finalizing data backup...", current, total_items)

        # Create assets ZIP if any cache toggles are enabled
        assets_path_str = None
        if assets_file and assets_items:
            if file_callback:
                file_callback(str(assets_file))
            if progress_callback:
                progress_callback("Backing up image cache...", current, total_items)

            with zipfile.ZipFile(assets_file, 'w', zipfile.ZIP_DEFLATED) as zf:
                for archive_name, source_path in assets_items:
                    data = source_path.read_bytes()
                    zf.writestr(archive_name, data)
                    current += 1
                    if progress_callback:
                        progress_callback(
                            f"Backing up {archive_name}...", current, total_items,
                        )

            assets_path_str = str(assets_file)
            logger.info(f"Assets backup created: {assets_file}")

        # Update last backup timestamp
        config.set("backup.last_backup", datetime.now().isoformat())
        config.save()

        # Apply retention policy
        apply_retention_policy(config)

        logger.info(f"Backup created: {backup_file}")
        return True, str(backup_file), assets_path_str

    except Exception as e:
        logger.error(f"Failed to create backup: {e}")
        return False, str(e), None


def verify_backup(backup_path: Path) -> Tuple[bool, List[str]]:
    """Verify integrity of a backup using SHA256 checksums.

    Args:
        backup_path: Path to the backup ZIP file.

    Returns:
        Tuple of (all_ok, problems).
        Old backups without checksums return (True, []).
    """
    try:
        with zipfile.ZipFile(backup_path, 'r') as zf:
            if "backup_info.json" not in zf.namelist():
                return False, ["Missing backup_info.json"]

            backup_info = json.loads(zf.read("backup_info.json"))
            files = backup_info.get("files", [])

            # Old format (list) — no checksums available, skip verification
            if isinstance(files, list):
                return True, []

            # New format (dict) — verify each file
            problems = []
            for name, meta in files.items():
                expected_sha = meta.get("sha256", "")
                expected_size = meta.get("size")

                if name not in zf.namelist():
                    problems.append(f"{name}: missing from archive")
                    continue

                data = zf.read(name)

                if expected_size is not None and len(data) != expected_size:
                    problems.append(
                        f"{name}: size mismatch "
                        f"(expected {expected_size}, got {len(data)})"
                    )

                if expected_sha:
                    actual_sha = hashlib.sha256(data).hexdigest()
                    if actual_sha != expected_sha:
                        problems.append(
                            f"{name}: checksum mismatch"
                        )

            return len(problems) == 0, problems

    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as e:
        return False, [f"Failed to read backup: {e}"]
    except Exception as e:
        return False, [f"Verification error: {e}"]


def apply_retention_policy(config: Config) -> int:
    """Apply GFS retention policy to existing backups.

    Args:
        config: Application config

    Returns:
        Number of backups deleted.
    """
    backup_dir = get_backup_dir(config)
    if not backup_dir.exists():
        return 0

    # Get all backups sorted by date (newest first)
    backups = []
    for f in backup_dir.glob("luducat_backup_*.zip"):
        try:
            # Parse timestamp from filename
            name = f.stem
            ts_str = name.replace("luducat_backup_", "")
            ts = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            backups.append((ts, f))
        except ValueError:
            continue

    backups.sort(key=lambda x: x[0], reverse=True)

    if not backups:
        return 0

    now = datetime.now()
    keep_daily = config.get("backup.retention_daily", 7)
    keep_weekly = config.get("backup.retention_weekly", 4)
    keep_monthly = config.get("backup.retention_monthly", 12)
    keep_yearly = config.get("backup.retention_yearly", 1)

    # Categorize backups
    to_keep = set()
    daily_kept = 0
    weekly_kept: Dict[Tuple[int, int], int] = defaultdict(int)
    monthly_kept: Dict[Tuple[int, int], int] = defaultdict(int)
    yearly_kept: Dict[int, int] = defaultdict(int)

    for ts, path in backups:
        # Daily: keep last N days
        days_ago = (now - ts).days
        if days_ago < keep_daily and daily_kept < keep_daily:
            to_keep.add(path)
            daily_kept += 1

        # Weekly: keep one per week for last N weeks
        if keep_weekly > 0:
            year_week = ts.isocalendar()[:2]
            weeks_ago = (now - ts).days // 7
            if weeks_ago < keep_weekly * 2 and weekly_kept[year_week] == 0:
                if len([k for k in weekly_kept.values() if k > 0]) < keep_weekly:
                    to_keep.add(path)
                    weekly_kept[year_week] = 1

        # Monthly: keep one per month for last N months
        if keep_monthly > 0:
            year_month = (ts.year, ts.month)
            months_ago = (now.year - ts.year) * 12 + (now.month - ts.month)
            if months_ago < keep_monthly and monthly_kept[year_month] == 0:
                to_keep.add(path)
                monthly_kept[year_month] = 1

        # Yearly: keep one per year for last N years
        if keep_yearly > 0:
            year = ts.year
            years_ago = now.year - ts.year
            if years_ago < keep_yearly and yearly_kept[year] == 0:
                to_keep.add(path)
                yearly_kept[year] = 1

    # Delete backups not in keep set (and their companion assets ZIPs)
    deleted = 0
    for ts, path in backups:
        if path not in to_keep:
            try:
                path.unlink()
                deleted += 1
                logger.debug(f"Deleted old backup: {path.name}")
                # Also delete companion assets ZIP if present
                ts_suffix = path.stem.replace("luducat_backup_", "")
                companion = path.parent / f"luducat_assets_{ts_suffix}.zip"
                if companion.exists():
                    companion.unlink()
                    logger.debug(f"Deleted companion assets: {companion.name}")
            except Exception as e:
                logger.warning(f"Failed to delete backup {path}: {e}")

    if deleted > 0:
        logger.info(f"Retention policy: deleted {deleted} old backup(s)")

    return deleted


def categorize_backups(config: Config) -> Dict[str, Dict]:
    """Categorize existing backups into daily/weekly/monthly/yearly buckets.

    Uses the same GFS logic as apply_retention_policy to determine which
    bucket each backup falls into.

    Returns:
        Dict with keys 'daily', 'weekly', 'monthly', 'yearly', each containing:
            count: int, total_size: int (bytes), newest: Optional[datetime]
    """
    backup_dir = get_backup_dir(config)
    result = {
        "daily": {"count": 0, "total_size": 0, "newest": None},
        "weekly": {"count": 0, "total_size": 0, "newest": None},
        "monthly": {"count": 0, "total_size": 0, "newest": None},
        "yearly": {"count": 0, "total_size": 0, "newest": None},
    }

    if not backup_dir.exists():
        return result

    # Get all backups sorted by date (newest first)
    backups = []
    for f in backup_dir.glob("luducat_backup_*.zip"):
        try:
            name = f.stem
            ts_str = name.replace("luducat_backup_", "")
            ts = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            size = f.stat().st_size
            # Include companion assets ZIP size if present
            companion = f.parent / f"luducat_assets_{ts_str}.zip"
            if companion.exists():
                try:
                    size += companion.stat().st_size
                except OSError:
                    pass
            backups.append((ts, f, size))
        except (ValueError, OSError):
            continue

    backups.sort(key=lambda x: x[0], reverse=True)

    if not backups:
        return result

    now = datetime.now()
    keep_daily = config.get("backup.retention_daily", 7)
    keep_weekly = config.get("backup.retention_weekly", 4)
    keep_monthly = config.get("backup.retention_monthly", 12)
    keep_yearly = config.get("backup.retention_yearly", 1)

    # Track which backups go to which category (a backup can only be in one)
    categorized = set()
    daily_kept = 0
    weekly_kept: Dict[tuple, int] = defaultdict(int)
    monthly_kept: Dict[tuple, int] = defaultdict(int)
    yearly_kept: Dict[int, int] = defaultdict(int)

    for ts, path, size in backups:
        days_ago = (now - ts).days
        if days_ago < keep_daily and daily_kept < keep_daily:
            result["daily"]["count"] += 1
            result["daily"]["total_size"] += size
            if result["daily"]["newest"] is None:
                result["daily"]["newest"] = ts
            categorized.add(path)
            daily_kept += 1
            continue

        if keep_weekly > 0:
            year_week = ts.isocalendar()[:2]
            weeks_ago = (now - ts).days // 7
            if weeks_ago < keep_weekly * 2 and weekly_kept[year_week] == 0:
                if len([k for k in weekly_kept.values() if k > 0]) < keep_weekly:
                    result["weekly"]["count"] += 1
                    result["weekly"]["total_size"] += size
                    if result["weekly"]["newest"] is None:
                        result["weekly"]["newest"] = ts
                    categorized.add(path)
                    weekly_kept[year_week] = 1
                    continue

        if keep_monthly > 0:
            year_month = (ts.year, ts.month)
            months_ago = (now.year - ts.year) * 12 + (now.month - ts.month)
            if months_ago < keep_monthly and monthly_kept[year_month] == 0:
                result["monthly"]["count"] += 1
                result["monthly"]["total_size"] += size
                if result["monthly"]["newest"] is None:
                    result["monthly"]["newest"] = ts
                categorized.add(path)
                monthly_kept[year_month] = 1
                continue

        if keep_yearly > 0:
            year = ts.year
            years_ago = now.year - ts.year
            if years_ago < keep_yearly and yearly_kept[year] == 0:
                result["yearly"]["count"] += 1
                result["yearly"]["total_size"] += size
                if result["yearly"]["newest"] is None:
                    result["yearly"]["newest"] = ts
                categorized.add(path)
                yearly_kept[year] = 1
                continue

    return result


def get_backup_status(config: Config) -> Dict:
    """Get current backup status.

    Returns:
        Dict with last_backup, next_backup, backup_count, total_size
    """
    backup_dir = get_backup_dir(config)

    status = {
        "last_backup": None,
        "next_backup": None,
        "backup_count": 0,
        "total_size": 0,
    }

    # Last backup
    last_backup_str = config.get("backup.last_backup", "")
    if last_backup_str:
        try:
            status["last_backup"] = datetime.fromisoformat(last_backup_str)
        except ValueError:
            pass

    # Next backup
    if config.get("backup.schedule_enabled", False) and status["last_backup"]:
        interval = config.get("backup.interval_days", 1)
        status["next_backup"] = status["last_backup"] + timedelta(days=interval)

    # Backup count and size
    if backup_dir.exists():
        backups = list(backup_dir.glob("luducat_backup_*.zip"))
        status["backup_count"] = len(backups)
        status["total_size"] = sum(f.stat().st_size for f in backups)

    return status
