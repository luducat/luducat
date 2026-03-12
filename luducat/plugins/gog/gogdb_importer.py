# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# gogdb_importer.py

"""GOGdb Database Importer

Downloads and imports game data from GOGdb.org into the plugin's catalog database.

GOGdb provides daily dumps of the entire GOG product database:
- URL: https://www.gogdb.org/backups_v3/products/{YYYY-MM}/gogdb_{YYYY-MM-DD}.tar.xz
- Contents: product.json files for each GOG product

Import Process:
1. Download current day's dump (with fallback to previous days)
2. Decompress .tar.xz archive
3. Parse product.json files
4. Insert new games into catalog.db (skip existing)
5. Cleanup temporary files
"""

from luducat.plugins.sdk.json import json
import logging
import lzma
import os
import re
import shutil
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from luducat.plugins.sdk.datetime import utc_now

from .database import GogDatabase, GogGame

logger = logging.getLogger(__name__)


# GOGdb dump URL pattern
GOGDB_BASE_URL = "https://www.gogdb.org/backups_v3/products"

# Estimated sizes for disk space checks
# Based on typical GOGdb dump sizes (as of 2024-2025)
ESTIMATED_COMPRESSED_SIZE_MB = 150  # ~150MB compressed
ESTIMATED_DECOMPRESSED_SIZE_MB = 1500  # ~1.5GB decompressed
ESTIMATED_DB_GROWTH_MB = 200  # ~200MB database growth
DISK_SPACE_SAFETY_MARGIN = 1.2  # 20% safety margin


class PreflightError(Exception):
    """Raised when preflight checks fail"""
    pass


class PreflightResult:
    """Result of preflight checks"""

    def __init__(self):
        self.success = True
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.dump_url: Optional[str] = None
        self.dump_date: Optional[str] = None
        self.dump_size_bytes: int = 0
        self.available_space_bytes: int = 0
        self.required_space_bytes: int = 0

    def add_error(self, message: str) -> None:
        self.errors.append(message)
        self.success = False

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def format_size(self, bytes_val: int) -> str:
        """Format bytes as human-readable string"""
        if bytes_val >= 1024 * 1024 * 1024:
            return f"{bytes_val / (1024 * 1024 * 1024):.1f} GB"
        elif bytes_val >= 1024 * 1024:
            return f"{bytes_val / (1024 * 1024):.1f} MB"
        elif bytes_val >= 1024:
            return f"{bytes_val / 1024:.1f} KB"
        return f"{bytes_val} bytes"

    def get_summary(self) -> str:
        """Get human-readable summary"""
        lines = []
        if self.dump_url:
            lines.append(f"Dump: {self.dump_date}")
            lines.append(f"Download size: {self.format_size(self.dump_size_bytes)}")
        lines.append(f"Required space: {self.format_size(self.required_space_bytes)}")
        lines.append(f"Available space: {self.format_size(self.available_space_bytes)}")

        if self.errors:
            lines.append("")
            lines.append("Errors:")
            for error in self.errors:
                lines.append(f"  - {error}")

        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            for warning in self.warnings:
                lines.append(f"  - {warning}")

        return "\n".join(lines)


class ImportProgress:
    """Progress tracking for GOGdb import"""

    def __init__(self, callback: Optional[Callable[[str, int, int], None]] = None):
        """Initialize progress tracker

        Args:
            callback: Function(phase, current, total) for progress updates
        """
        self.callback = callback
        self.phase = ""
        self.current = 0
        self.total = 0

    def update(self, phase: str, current: int, total: int = 0) -> None:
        """Update progress"""
        self.phase = phase
        self.current = current
        self.total = total
        if self.callback:
            self.callback(phase, current, total)

    def increment(self) -> None:
        """Increment current progress"""
        self.current += 1
        if self.callback:
            self.callback(self.phase, self.current, self.total)


class GogdbImporter:
    """Imports GOGdb dumps into the plugin catalog

    Usage:
        importer = GogdbImporter(data_dir)

        def progress(phase, current, total):
            print(f"{phase}: {current}/{total}")

        stats = await importer.import_from_gogdb(progress)
        print(f"Imported {stats['imported']} games")
    """

    def __init__(self, data_dir: Path, cache_dir: Path, http_client=None):
        """Initialize importer

        Args:
            data_dir: Plugin's data directory (for catalog.db)
            cache_dir: Plugin's cache directory (for temp files)
            http_client: PluginHttpClient for all HTTP requests
        """
        self.data_dir = data_dir
        self.cache_dir = cache_dir
        self._http = http_client
        self.db: Optional[GogDatabase] = None

    def _get_db(self) -> GogDatabase:
        """Get or create database connection"""
        if self.db is None:
            self.db = GogDatabase(self.data_dir / "catalog.db")
            self.db.initialize()
        return self.db

    def get_dump_url(self, days_back: int = 0) -> str:
        """Build GOGdb dump URL

        Args:
            days_back: Number of days back from today

        Returns:
            Full URL to dump file
        """
        target_date = datetime.now() - timedelta(days=days_back)
        year_month = target_date.strftime("%Y-%m")
        date_str = target_date.strftime("%Y-%m-%d")
        return f"{GOGDB_BASE_URL}/{year_month}/gogdb_{date_str}.tar.xz"

    def preflight_check(self) -> PreflightResult:
        """Run preflight checks before import

        Checks:
        1. GOGdb dump is available for download
        2. Sufficient disk space for download, decompression, and import

        Returns:
            PreflightResult with success status and details
        """
        result = PreflightResult()

        # Check 1: Find available dump
        logger.info("Checking GOGdb dump availability...")
        dump_info = self._check_dump_availability()

        if not dump_info:
            result.add_error(
                "GOGdb dump not available. Tried last 4 days. "
                "The service may be temporarily unavailable."
            )
            return result

        result.dump_url = dump_info["url"]
        result.dump_date = dump_info["date"]
        result.dump_size_bytes = dump_info["size"]

        # Check 2: Calculate required disk space
        # Need space for: download + decompression + temp files
        # Download goes to cache_dir, extraction to temp, DB in data_dir
        if result.dump_size_bytes > 0:
            # Use actual size if available
            compressed_size = result.dump_size_bytes
        else:
            # Use estimate
            compressed_size = ESTIMATED_COMPRESSED_SIZE_MB * 1024 * 1024

        # Decompression ratio for .tar.xz is typically 10:1
        decompressed_size = compressed_size * 10

        # Total required: compressed + decompressed + DB growth + safety margin
        result.required_space_bytes = int(
            (compressed_size + decompressed_size + ESTIMATED_DB_GROWTH_MB * 1024 * 1024)
            * DISK_SPACE_SAFETY_MARGIN
        )

        # Check disk space in cache_dir (for downloads) and temp dir (for extraction)
        cache_space = self._get_available_space(self.cache_dir)
        temp_space = self._get_available_space(Path(tempfile.gettempdir()))
        data_space = self._get_available_space(self.data_dir)

        # Use minimum available space across all locations
        result.available_space_bytes = min(cache_space, temp_space, data_space)

        logger.info(
            f"Disk space check: required={result.format_size(result.required_space_bytes)}, "
            f"available={result.format_size(result.available_space_bytes)}"
        )

        if result.available_space_bytes < result.required_space_bytes:
            result.add_error(
                f"Insufficient disk space. "
                f"Required: {result.format_size(result.required_space_bytes)}, "
                f"Available: {result.format_size(result.available_space_bytes)}"
            )

        # Warnings for low space
        if result.available_space_bytes < result.required_space_bytes * 1.5:
            result.add_warning("Disk space is limited. Consider freeing up space before import.")

        return result

    def _check_dump_availability(self) -> Optional[Dict[str, Any]]:
        """Check if GOGdb dump is available for download

        Tries HEAD requests for current day and up to 3 days back.

        Returns:
            Dict with url, date, size or None if not available
        """
        for days_back in range(4):
            url = self.get_dump_url(days_back)
            target_date = datetime.now() - timedelta(days=days_back)
            date_str = target_date.strftime("%Y-%m-%d")

            try:
                # Use HEAD request to check availability without downloading
                response = self._http.head(url, timeout=15, allow_redirects=True)

                if response.status_code == 200:
                    # Get file size from Content-Length header
                    size = int(response.headers.get("content-length", 0))
                    logger.info(f"Found GOGdb dump: {date_str} ({size} bytes)")
                    return {
                        "url": url,
                        "date": date_str,
                        "size": size,
                    }
                else:
                    logger.debug(f"Dump not available for {date_str}: HTTP {response.status_code}")

            except Exception as e:
                logger.debug(f"Failed to check {url}: {e}")
                continue

        return None

    def _get_available_space(self, path: Path) -> int:
        """Get available disk space for a path

        Args:
            path: Path to check

        Returns:
            Available space in bytes
        """
        try:
            # Ensure path exists
            path.mkdir(parents=True, exist_ok=True)

            # Use shutil.disk_usage for cross-platform support
            usage = shutil.disk_usage(path)
            return usage.free
        except Exception as e:
            logger.warning(f"Failed to get disk space for {path}: {e}")
            # Return a large value to not block on error
            return 100 * 1024 * 1024 * 1024  # 100GB

    def import_from_gogdb(
        self,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        skip_preflight: bool = False
    ) -> Dict[str, Any]:
        """Import GOGdb dump into catalog

        Downloads the latest GOGdb dump, extracts it, and imports
        new games into the catalog database.

        Args:
            progress_callback: Function(phase, current, total) for updates
            skip_preflight: If True, skip preflight checks (use if already done)

        Returns:
            Stats dict with keys:
            - downloaded: bool
            - dump_date: str
            - products_found: int
            - imported: int
            - skipped: int
            - errors: int
            - preflight_failed: bool (if preflight checks failed)
            - preflight_errors: list (error messages if failed)
        """
        progress = ImportProgress(progress_callback)
        stats = {
            "downloaded": False,
            "dump_date": "",
            "products_found": 0,
            "imported": 0,
            "skipped": 0,
            "errors": 0,
            "preflight_failed": False,
            "preflight_errors": [],
        }

        # Run preflight checks
        if not skip_preflight:
            progress.update("Running preflight checks...", 0, 0)
            preflight = self.preflight_check()

            if not preflight.success:
                stats["preflight_failed"] = True
                stats["preflight_errors"] = preflight.errors
                logger.error(f"Preflight checks failed: {preflight.errors}")
                return stats

            # Log warnings but continue
            for warning in preflight.warnings:
                logger.warning(f"Preflight warning: {warning}")

        temp_dir = None
        try:
            # Phase 1: Download dump
            progress.update("Downloading GOGdb dump...", 0, 0)
            dump_path, dump_date = self._download_dump(progress)

            if not dump_path:
                logger.error("Failed to download GOGdb dump")
                return stats

            stats["downloaded"] = True
            stats["dump_date"] = dump_date

            # Phase 2: Extract dump
            progress.update("Extracting archive...", 0, 0)
            temp_dir = self._extract_dump(dump_path, progress)

            if not temp_dir:
                logger.error("Failed to extract GOGdb dump")
                return stats

            # Phase 3: Find product files
            progress.update("Scanning products...", 0, 0)
            product_files, all_ids = self._find_product_files(temp_dir)
            stats["products_found"] = len(product_files)

            if not product_files:
                logger.warning("No product files found in dump")
                return stats

            # Phase 4: Import products (only games, skip DLCs etc.)
            progress.update("Importing games...", 0, len(product_files))
            import_stats = self._import_products(product_files, progress)

            stats["imported"] = import_stats["imported"]
            stats["skipped"] = import_stats["skipped"]
            stats["skipped_non_game"] = import_stats.get("skipped_non_game", 0)
            stats["errors"] = import_stats["errors"]

            logger.info(
                f"GOGdb import complete: {stats['imported']} imported, "
                f"{stats['skipped']} skipped, {stats['errors']} errors"
            )

        except Exception as e:
            logger.error(f"GOGdb import failed: {e}")
            raise

        finally:
            # Cleanup temp directory
            if temp_dir and temp_dir.exists():
                try:
                    shutil.rmtree(temp_dir)
                    logger.debug(f"Cleaned up temp directory: {temp_dir}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp dir: {e}")

            # Close database
            if self.db:
                self.db.close()
                self.db = None

        return stats

    def _download_dump(
        self, progress: ImportProgress
    ) -> Tuple[Optional[Path], str]:
        """Download GOGdb dump file

        Tries current day, then falls back up to 3 days.

        Returns:
            Tuple of (path to downloaded file, dump date string)
        """
        download_dir = self.cache_dir / "gogdb_downloads"
        download_dir.mkdir(parents=True, exist_ok=True)

        for days_back in range(4):  # Try today and 3 days back
            url = self.get_dump_url(days_back)
            target_date = datetime.now() - timedelta(days=days_back)
            date_str = target_date.strftime("%Y-%m-%d")
            filename = f"gogdb_{date_str}.tar.xz"
            dest_path = download_dir / filename

            # Check if already downloaded
            if dest_path.exists():
                logger.info(f"Using cached dump: {dest_path}")
                return dest_path, date_str

            logger.info(f"Trying to download: {url}")
            progress.update(f"Downloading ({date_str})...", 0, 0)

            try:
                response = self._http.get(url, stream=True, timeout=30)
                response.raise_for_status()

                # Get file size for progress
                total_size = int(response.headers.get("content-length", 0))
                downloaded = 0

                with open(dest_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            progress.update(
                                f"Downloading ({date_str})...",
                                downloaded,
                                total_size
                            )

                logger.info(f"Downloaded: {dest_path}")
                return dest_path, date_str

            except Exception as e:
                logger.debug(f"Failed to download {url}: {e}")
                # Clean up partial download
                if dest_path.exists():
                    dest_path.unlink()
                continue

        logger.error("Failed to download GOGdb dump (tried 4 days)")
        return None, ""

    def _extract_dump(
        self, dump_path: Path, progress: ImportProgress
    ) -> Optional[Path]:
        """Extract .tar.xz dump to temporary directory

        Returns:
            Path to extracted directory
        """
        temp_dir = Path(tempfile.mkdtemp(prefix="gogdb_import_"))

        try:
            progress.update("Decompressing archive...", 0, 0)

            # Open .tar.xz file
            with lzma.open(dump_path, "rb") as xz_file:
                with tarfile.open(fileobj=xz_file) as tar:
                    # Count members for progress
                    members = tar.getmembers()
                    total = len(members)
                    progress.update("Extracting files...", 0, total)

                    for i, member in enumerate(members):
                        tar.extract(member, temp_dir)
                        if i % 100 == 0:  # Update every 100 files
                            progress.update("Extracting files...", i, total)

            logger.info(f"Extracted dump to: {temp_dir}")
            return temp_dir

        except Exception as e:
            logger.error(f"Failed to extract dump: {e}")
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            return None

    def _find_product_files(self, extract_dir: Path) -> Tuple[List[Path], set]:
        """Find all product.json files in extracted dump

        Uses ids.json at root level to identify products, then locates
        product files at products/{gogid}/product.json

        Returns:
            Tuple of (list of product.json paths, set of all product IDs from ids.json)
        """
        # First try to load ids.json for efficient lookup
        all_ids = set()
        ids_json = extract_dir / "ids.json"
        if ids_json.exists():
            try:
                with open(ids_json, "r", encoding="utf-8") as f:
                    ids_data = json.load(f)
                    if isinstance(ids_data, list):
                        all_ids = set(ids_data)
                    logger.info(f"Found {len(all_ids)} product IDs in ids.json")
            except Exception as e:
                logger.warning(f"Failed to parse ids.json: {e}")

        # Find product files in products/{gogid}/product.json structure
        products_dir = extract_dir / "products"
        product_files = []

        if products_dir.exists():
            for product_dir in products_dir.iterdir():
                if product_dir.is_dir():
                    product_json = product_dir / "product.json"
                    if product_json.exists():
                        product_files.append(product_json)
        else:
            # Fallback: search for product.json files anywhere
            product_files = list(extract_dir.rglob("product.json"))

        logger.info(f"Found {len(product_files)} product files")
        return product_files, all_ids

    def _import_products(
        self, product_files: List[Path], progress: ImportProgress
    ) -> Dict[str, int]:
        """Import product files into database

        Only imports products where type == "game".
        DLCs, packs, soundtracks etc. are skipped.

        Returns:
            Stats dict with imported, skipped, skipped_non_game, errors counts
        """
        db = self._get_db()
        stats = {"imported": 0, "skipped": 0, "skipped_non_game": 0, "errors": 0}

        # Get existing IDs for fast skip check
        existing_ids = set(db.get_all_gogids(include_dlc=True))

        batch = []
        batch_size = 100

        for i, product_file in enumerate(product_files):
            try:
                with open(product_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                gogid = data.get("id")
                if not gogid:
                    stats["errors"] += 1
                    continue

                # Check if it's a game before checking if exists
                product_type = data.get("type", "").lower()
                if product_type != "game":
                    stats["skipped_non_game"] += 1
                    continue

                # Skip existing games
                if gogid in existing_ids:
                    stats["skipped"] += 1
                    if i % 100 == 0:
                        progress.update("Importing games...", i, len(product_files))
                    continue

                # Try to load prices from prices.json in same directory
                prices_file = product_file.parent / "prices.json"
                price_data = None
                if prices_file.exists():
                    try:
                        with open(prices_file, "r", encoding="utf-8") as pf:
                            price_data = json.load(pf)
                    except Exception:
                        pass  # Price data is optional

                # Parse and create game
                game = self._parse_product(data, price_data)
                if game:
                    batch.append(game)
                    existing_ids.add(gogid)  # Track for duplicate detection
                else:
                    # _parse_product returns None for non-games, but we already filtered
                    stats["errors"] += 1

                # Commit batch
                if len(batch) >= batch_size:
                    for g in batch:
                        db.upsert_game(g)
                    db.commit()
                    stats["imported"] += len(batch)
                    batch = []

            except Exception as e:
                logger.debug(f"Error parsing {product_file}: {e}")
                stats["errors"] += 1

            if i % 100 == 0:
                progress.update("Importing games...", i, len(product_files))

        # Commit remaining batch
        if batch:
            for g in batch:
                db.upsert_game(g)
            db.commit()
            stats["imported"] += len(batch)

        progress.update("Import complete", len(product_files), len(product_files))
        return stats

    def _parse_product(
        self, data: Dict[str, Any], price_data: Optional[Dict[str, Any]] = None
    ) -> Optional[GogGame]:
        """Parse GOGdb product.json into GogGame model

        GOGdb dump structure (actual format):
        - id: int (GOG product ID)
        - title: str
        - type: str ("game", "dlc", "pack", etc.)
        - slug: str
        - description: str (HTML content, NOT a dict)
        - developers: list[str] (list of developer names)
        - publisher: str (single publisher, NOT a list)
        - tags: list[{id, level, name, slug}]
        - features: list[{id, name}]
        - screenshots: list[str] (image hashes, NOT dicts)
        - image_background, image_logo, image_icon, etc.: str (image hashes at root)
        - comp_systems / cs_systems: list[str] (["windows", "osx", "linux"])
        - global_date / store_date: str (ISO date)
        - store_state: str ("default" = available)

        prices.json structure (separate file):
        - {region: {currency: [{date, price_base, price_final}, ...]}}
        - We use US/USD and take newest price_base

        Args:
            data: Parsed product.json data
            price_data: Optional parsed prices.json data

        Returns:
            GogGame instance or None if invalid/not a game
        """
        try:
            gogid = data.get("id")
            if not gogid:
                return None

            # Check product type - only import games
            product_type = data.get("type", "").lower()
            if product_type != "game":
                # Skip DLCs, packs, soundtracks, etc.
                return None

            game = GogGame(
                gogid=gogid,
                slug=data.get("slug", ""),
                title=data.get("title", f"Game {gogid}"),
                type=product_type,
            )

            # Description is a string with HTML, not a dict
            description = data.get("description", "")
            if isinstance(description, str):
                game.description = description
                # Extract short description from first paragraph
                if description:
                    # Strip HTML and get first ~200 chars
                    text = re.sub(r'<[^>]+>', ' ', description)
                    text = ' '.join(text.split())[:200]
                    game.short_description = text
            elif isinstance(description, dict):
                # Fallback for older format
                game.description = description.get("full", "")
                game.short_description = description.get("lead", "")

            # Release date - prefer global_date, fallback to store_date
            release = data.get("global_date") or data.get("store_date", "")
            if release:
                # Parse ISO date to just date part
                if "T" in str(release):
                    game.release_date = str(release).split("T")[0]
                else:
                    game.release_date = str(release)

            # Developers - list of strings (not dicts)
            developers = data.get("developers", [])
            if developers:
                if isinstance(developers[0], str):
                    game.developers = developers
                elif isinstance(developers[0], dict):
                    game.developers = [d.get("name", "") for d in developers if d.get("name")]

            # Publisher - single string (not a list)
            publisher = data.get("publisher", "")
            if publisher:
                game.publishers = [publisher] if isinstance(publisher, str) else []

            # Tags - list of {id, level, name, slug}
            tags = data.get("tags", [])
            if tags and isinstance(tags[0], dict):
                game.tags = [t.get("name", "") for t in tags if t.get("name")]
            else:
                game.tags = []

            # Features - list of {id, name}
            features = data.get("features", [])
            if features and isinstance(features[0], dict):
                game.features = [f.get("name", "") for f in features if f.get("name")]
            else:
                game.features = []

            # Genres - may not exist in all products
            genres = data.get("genres", [])
            if genres:
                if isinstance(genres[0], dict):
                    game.genres = [g.get("name", "") for g in genres if g.get("name")]
                else:
                    game.genres = genres

            # Images - hashes at root level, not in nested dict
            game.cover_url = self._build_image_url(data.get("image_boxart"))
            game.background_url = self._build_image_url(data.get("image_background"))
            game.logo_url = self._build_image_url(data.get("image_logo"))
            game.icon_url = self._build_image_url(data.get("image_icon"))
            game.galaxy_background_url = self._build_image_url(data.get("image_galaxy_background"))
            game.icon_square_url = self._build_image_url(data.get("image_icon_square"))

            # Screenshots - list of image hashes (strings, not dicts)
            screenshots = data.get("screenshots", [])
            if screenshots:
                if isinstance(screenshots[0], str):
                    # Direct hash strings
                    game.screenshots = [self._build_image_url(h) for h in screenshots if h]
                elif isinstance(screenshots[0], dict):
                    # Fallback for {image_id: ...} format
                    game.screenshots = [
                        self._build_image_url(ss.get("image_id"))
                        for ss in screenshots if ss.get("image_id")
                    ]
            else:
                game.screenshots = []

            # Platform support - comp_systems or cs_systems is a list like ["windows", "osx"]
            platforms = data.get("comp_systems") or data.get("cs_systems", [])
            game.windows = "windows" in platforms
            game.mac = "osx" in platforms or "mac" in platforms
            game.linux = "linux" in platforms

            # Status - store_state "default" means available
            game.is_available = data.get("store_state", "default") == "default"
            game.gogdb_imported = True
            game.last_updated = utc_now()

            # Series/franchise - nested dict {id, name}
            series = data.get("series")
            if isinstance(series, dict):
                game.series_name = series.get("name")
                game.series_id = series.get("id")

            # Age rating from v2
            age_rating = data.get("age_rating")
            if isinstance(age_rating, int):
                game.age_rating = age_rating

            # User rating from catalog (0-50 scale)
            user_rating = data.get("user_rating")
            if isinstance(user_rating, (int, float)):
                game.rating = int(user_rating)

            # Reviews count from catalog
            reviews_count = data.get("reviews_count")
            if isinstance(reviews_count, int):
                game.reviews_count = reviews_count

            # Localizations - list of {code, name, text, audio}
            localizations = data.get("localizations", [])
            if localizations and isinstance(localizations, list):
                game.localizations = localizations

            # Editions - list of {id, name, ...}
            editions = data.get("editions", [])
            if editions and isinstance(editions, list):
                game.editions = editions

            # Pack/bundle relationships
            includes = data.get("includes_games", [])
            if includes and isinstance(includes, list):
                game.includes_games = includes
            is_included = data.get("is_included_in", [])
            if is_included and isinstance(is_included, list):
                game.is_included_in = is_included

            # DLC dependencies
            req_by = data.get("required_by", [])
            if req_by and isinstance(req_by, list):
                game.required_by = req_by
            requires = data.get("requires", [])
            if requires and isinstance(requires, list):
                game.requires = requires

            # Copyright
            copyright_val = data.get("copyright")
            if isinstance(copyright_val, str) and copyright_val:
                game.copyright_notice = copyright_val[:500]

            # Runtime flags
            if data.get("is_using_dosbox") is not None:
                game.is_using_dosbox = bool(data["is_using_dosbox"])
            if data.get("is_in_development") is not None:
                game.is_in_development = bool(data["is_in_development"])

            # Rankings
            rank_bs = data.get("rank_bestselling")
            if isinstance(rank_bs, int):
                game.rank_bestselling = rank_bs
            rank_tr = data.get("rank_trending")
            if isinstance(rank_tr, int):
                game.rank_trending = rank_tr

            # Dates — store_date vs global_date
            store_date = data.get("store_date", "")
            if store_date:
                if "T" in str(store_date):
                    game.store_release_date = str(store_date).split("T")[0]
                else:
                    game.store_release_date = str(store_date)[:10]
            global_date = data.get("global_date", "")
            if global_date:
                if "T" in str(global_date):
                    game.global_release_date = str(global_date).split("T")[0]
                else:
                    game.global_release_date = str(global_date)[:10]

            # Links
            link_store = data.get("link_store")
            if isinstance(link_store, str) and link_store:
                game.store_link = link_store
            link_forum = data.get("link_forum")
            if isinstance(link_forum, str) and link_forum:
                game.forum_link = link_forum
            link_support = data.get("link_support")
            if isinstance(link_support, str) and link_support:
                game.support_link = link_support

            # Price from prices.json (if provided)
            if price_data:
                price_base = self._extract_price(price_data)
                if price_base is not None:
                    game.price = price_base / 100  # Cents to dollars
                    game.is_free = price_base == 0

            return game

        except Exception as e:
            logger.debug(f"Error parsing product {data.get('id')}: {e}")
            return None

    def _build_image_url(self, image_id: Optional[str]) -> Optional[str]:
        """Build GOG CDN URL from image ID

        Args:
            image_id: GOG image hash/ID

        Returns:
            Full CDN URL or None
        """
        if not image_id:
            return None

        # GOG CDN URL pattern
        return f"https://images.gog-statics.com/{image_id}.jpg"

    def _extract_price(self, price_data: Dict[str, Any]) -> Optional[int]:
        """Extract price_base from prices.json data

        prices.json structure:
        {
          "US": {
            "USD": [
              {"currency": "USD", "date": "...", "price_base": 1499, "price_final": 1199},
              ...
            ]
          },
          ...
        }

        We use US/USD and take the newest (first in list) price_base.

        Args:
            price_data: Parsed prices.json content

        Returns:
            price_base in cents, or None if not found
        """
        try:
            # Get US region prices
            us_prices = price_data.get("US", {})
            if not us_prices:
                return None

            # Get USD currency prices
            usd_prices = us_prices.get("USD", [])
            if not usd_prices:
                return None

            # Prices are sorted by date (newest first), take first entry
            if isinstance(usd_prices, list) and len(usd_prices) > 0:
                newest = usd_prices[0]
                return newest.get("price_base")

            return None
        except Exception:
            return None

    def fetch_product_from_api(self, gogid: int) -> Optional[GogGame]:
        """Fetch a single product from GOGdb API

        Used for cache misses - when a game isn't in the local catalog but
        we need its metadata (e.g., during sync with owned games).

        Endpoint: https://www.gogdb.org/data/products/{gogid}/product.json
        Prices: https://www.gogdb.org/data/products/{gogid}/prices.json

        Args:
            gogid: GOG product ID

        Returns:
            GogGame instance or None if not found/invalid
        """
        base_url = "https://www.gogdb.org/data/products"
        product_url = f"{base_url}/{gogid}/product.json"
        prices_url = f"{base_url}/{gogid}/prices.json"

        try:
            # Fetch product.json
            logger.debug(f"Fetching product from GOGdb API: {gogid}")
            response = self._http.get(product_url, timeout=15)

            if response.status_code == 404:
                logger.debug(f"Product not found in GOGdb: {gogid}")
                return None

            response.raise_for_status()
            product_data = response.json()

            # Try to fetch prices.json (optional)
            price_data = None
            try:
                price_response = self._http.get(prices_url, timeout=10)
                if price_response.status_code == 200:
                    price_data = price_response.json()
            except Exception:
                pass  # Prices are optional

            # Parse and return
            game = self._parse_product(product_data, price_data)
            if game:
                logger.info(f"Fetched product from GOGdb API: {gogid} - {game.title}")
            return game

        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON for product {gogid}: {e}")
            return None
        except Exception as e:
            logger.warning(f"Failed to fetch product {gogid} from GOGdb API: {e}")
            return None

    def fetch_and_cache_product(self, gogid: int) -> Optional[GogGame]:
        """Fetch a product from API and save to local cache DB

        Convenience method that fetches from API and persists to catalog.db.

        Args:
            gogid: GOG product ID

        Returns:
            GogGame instance or None if not found
        """
        game = self.fetch_product_from_api(gogid)
        if game:
            db = self._get_db()
            db.upsert_game(game)
            db.commit()
            logger.info(f"Cached product in local DB: {gogid}")
        return game
