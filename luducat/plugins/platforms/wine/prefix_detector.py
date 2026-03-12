# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# prefix_detector.py

"""Wine Prefix Detection

Scans the filesystem for Wine/Proton prefixes created by various launchers.
Privacy-gated via local_data_consent.

Supported prefix sources:
- Steam Proton (compatdata)
- Heroic Games Launcher
- Lutris
- Bottles
- PlayOnLinux / PlayOnMac
- PortProton
- Faugus Launcher
- GameHub
- Generic Wine (~/.wine, ~/Games)
"""

import logging
import platform
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class WinePrefix:
    """Detected Wine/Proton prefix.

    Attributes:
        prefix_path: Path to the Wine prefix root (contains drive_c/).
        wine_binary: Path to the Wine binary used, if known.
        source: Launcher that created this prefix.
        source_version: Wine/Proton version string.
        store_name: Store the game belongs to (e.g. "steam", "gog").
        store_app_id: Store-specific app ID, if resolvable.
        arch: Windows architecture ("win64" or "win32").
        last_used: Last modification time, if known.
        environment: Environment variables from the source launcher config.
    """
    prefix_path: Path
    wine_binary: Optional[Path]
    source: str                 # "steam_proton", "heroic", "lutris", "bottles", etc.
    source_version: str
    store_name: str
    store_app_id: str
    arch: str = "win64"
    last_used: Optional[datetime] = None
    environment: Dict[str, str] = field(default_factory=dict)


class PrefixDetector:
    """Scans the system for Wine/Proton prefixes.

    All scanning is read-only. No filesystem mutations.
    """

    def __init__(self, local_data_consent: bool = False):
        self._consent = local_data_consent

    def scan_all(self) -> List[WinePrefix]:
        """Scan all known prefix sources.

        Returns:
            List of detected WinePrefix objects.
        """
        if not self._consent:
            logger.info("PrefixDetector: local data consent not granted, skipping")
            return []

        if platform.system() != "Linux":
            logger.debug("PrefixDetector: Wine prefix scanning is Linux-only")
            return []

        prefixes: List[WinePrefix] = []
        seen_paths: set = set()

        def _add(p: WinePrefix) -> None:
            key = str(p.prefix_path.resolve())
            if key not in seen_paths:
                seen_paths.add(key)
                prefixes.append(p)

        # Scan each source
        for scanner in (
            self._scan_steam_proton,
            self._scan_heroic,
            self._scan_lutris,
            self._scan_bottles,
            self._scan_playonlinux,
            self._scan_portproton,
            self._scan_faugus,
            self._scan_gamehub,
            self._scan_generic,
        ):
            try:
                for prefix in scanner():
                    _add(prefix)
            except Exception as e:
                logger.debug("PrefixDetector: %s failed: %s", scanner.__name__, e)

        logger.info("PrefixDetector: found %d Wine prefixes", len(prefixes))
        return prefixes

    # === STEAM PROTON ===

    def _scan_steam_proton(self) -> List[WinePrefix]:
        """Scan Steam Proton compatdata directories."""
        results = []

        steam_dirs = [
            Path.home() / ".steam" / "steam",
            Path.home() / ".local" / "share" / "Steam",
        ]

        for steam_dir in steam_dirs:
            compatdata = steam_dir / "steamapps" / "compatdata"
            if not compatdata.is_dir():
                continue

            try:
                for app_dir in compatdata.iterdir():
                    if not app_dir.is_dir():
                        continue

                    pfx = app_dir / "pfx"
                    if not self._is_valid_prefix(pfx):
                        continue

                    version = self._read_proton_version(app_dir)
                    arch = self._detect_prefix_arch(pfx)
                    last_used = self._get_prefix_mtime(pfx)

                    results.append(WinePrefix(
                        prefix_path=pfx,
                        wine_binary=None,  # Proton manages its own binary
                        source="steam_proton",
                        source_version=version,
                        store_name="steam",
                        store_app_id=app_dir.name,
                        arch=arch,
                        last_used=last_used,
                    ))
            except OSError:
                continue

        return results

    def _read_proton_version(self, compatdata_dir: Path) -> str:
        """Read Proton version from compatdata version file."""
        version_file = compatdata_dir / "version"
        if version_file.is_file():
            try:
                return version_file.read_text().strip()
            except OSError:
                pass
        return "unknown"

    # === HEROIC ===

    def _scan_heroic(self) -> List[WinePrefix]:
        """Scan Heroic Games Launcher game configs for Wine prefixes."""
        results = []

        config_dirs = [
            Path.home() / ".config" / "heroic" / "GamesConfig",
            (Path.home() / ".var" / "app" / "com.heroicgameslauncher.hgl"
             / "config" / "heroic" / "GamesConfig"),
        ]

        for config_dir in config_dirs:
            if not config_dir.is_dir():
                continue

            try:
                for config_file in config_dir.glob("*.json"):
                    prefix = self._parse_heroic_game_config(config_file)
                    if prefix:
                        results.append(prefix)
            except OSError:
                continue

        return results

    def _parse_heroic_game_config(self, config_path: Path) -> Optional[WinePrefix]:
        """Parse a Heroic game config JSON for prefix info."""
        import json

        try:
            data = json.loads(config_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

        prefix_path_str = data.get("winePrefix")
        if not prefix_path_str:
            return None

        prefix_path = Path(prefix_path_str).expanduser()
        if not self._is_valid_prefix(prefix_path):
            return None

        wine_version = data.get("wineVersion", {})
        version_name = wine_version.get("name", "unknown") if isinstance(wine_version, dict) else str(wine_version)
        wine_bin_str = wine_version.get("bin") if isinstance(wine_version, dict) else None
        wine_binary = Path(wine_bin_str) if wine_bin_str else None

        # Determine store from app ID pattern
        app_id = config_path.stem
        store_name = "epic"  # Heroic primarily handles Epic
        if app_id.isdigit():
            store_name = "gog"

        env = {}
        if data.get("envVariables"):
            env = data["envVariables"]

        return WinePrefix(
            prefix_path=prefix_path,
            wine_binary=wine_binary,
            source="heroic",
            source_version=version_name,
            store_name=store_name,
            store_app_id=app_id,
            arch=self._detect_prefix_arch(prefix_path),
            last_used=self._get_prefix_mtime(prefix_path),
            environment=env,
        )

    # === LUTRIS ===

    def _scan_lutris(self) -> List[WinePrefix]:
        """Scan Lutris game configs for Wine prefixes."""
        results = []

        games_dir = Path.home() / ".local" / "share" / "lutris" / "games"
        if not games_dir.is_dir():
            # Try pga.db approach — Lutris stores game info in SQLite
            return results

        # Lutris uses YAML configs
        try:
            import yaml
        except ImportError:
            # PyYAML not available, skip
            logger.debug("PrefixDetector: PyYAML not available, skipping Lutris scan")
            return results

        config_dir = Path.home() / ".config" / "lutris" / "games"
        if not config_dir.is_dir():
            return results

        try:
            for config_file in config_dir.glob("*.yml"):
                try:
                    data = yaml.safe_load(config_file.read_text())
                except Exception:
                    continue

                if not isinstance(data, dict):
                    continue

                game = data.get("game", {})
                system = data.get("system", {})

                prefix_str = game.get("prefix") or system.get("prefix")
                if not prefix_str:
                    continue

                prefix_path = Path(prefix_str).expanduser()
                if not self._is_valid_prefix(prefix_path):
                    continue

                wine_path = system.get("wine_path")
                wine_binary = Path(wine_path) if wine_path else None

                results.append(WinePrefix(
                    prefix_path=prefix_path,
                    wine_binary=wine_binary,
                    source="lutris",
                    source_version=system.get("version", "unknown"),
                    store_name="unknown",
                    store_app_id=config_file.stem,
                    arch=self._detect_prefix_arch(prefix_path),
                    last_used=self._get_prefix_mtime(prefix_path),
                ))
        except OSError:
            pass

        return results

    # === BOTTLES ===

    def _scan_bottles(self) -> List[WinePrefix]:
        """Scan Bottles for Wine prefixes."""
        results = []

        bottles_dirs = [
            Path.home() / ".local" / "share" / "bottles" / "bottles",
            (Path.home() / ".var" / "app" / "com.usebottles.bottles"
             / "data" / "bottles" / "bottles"),
        ]

        for bottles_dir in bottles_dirs:
            if not bottles_dir.is_dir():
                continue

            try:
                for bottle_dir in bottles_dir.iterdir():
                    if not bottle_dir.is_dir():
                        continue

                    # Bottles stores config in bottle.yml
                    if not self._is_valid_prefix(bottle_dir):
                        continue

                    version = "unknown"
                    bottle_yml = bottle_dir / "bottle.yml"
                    if bottle_yml.is_file():
                        try:
                            import yaml
                            data = yaml.safe_load(bottle_yml.read_text())
                            if isinstance(data, dict):
                                version = data.get("Runner", "unknown")
                        except Exception:
                            pass

                    results.append(WinePrefix(
                        prefix_path=bottle_dir,
                        wine_binary=None,
                        source="bottles",
                        source_version=version,
                        store_name="unknown",
                        store_app_id=bottle_dir.name,
                        arch=self._detect_prefix_arch(bottle_dir),
                        last_used=self._get_prefix_mtime(bottle_dir),
                    ))
            except OSError:
                continue

        return results

    # === PLAYONLINUX ===

    def _scan_playonlinux(self) -> List[WinePrefix]:
        """Scan PlayOnLinux/PlayOnMac prefixes."""
        results = []

        pol_dirs = [
            Path.home() / ".PlayOnLinux" / "wineprefix",
            Path.home() / "Library" / "PlayOnMac" / "wineprefix",
        ]

        for pol_dir in pol_dirs:
            if not pol_dir.is_dir():
                continue

            try:
                for prefix_dir in pol_dir.iterdir():
                    if not prefix_dir.is_dir():
                        continue

                    if not self._is_valid_prefix(prefix_dir):
                        continue

                    results.append(WinePrefix(
                        prefix_path=prefix_dir,
                        wine_binary=None,
                        source="playonlinux",
                        source_version="unknown",
                        store_name="unknown",
                        store_app_id=prefix_dir.name,
                        arch=self._detect_prefix_arch(prefix_dir),
                        last_used=self._get_prefix_mtime(prefix_dir),
                    ))
            except OSError:
                continue

        return results

    # === PORTPROTON ===

    def _scan_portproton(self) -> List[WinePrefix]:
        """Scan PortProton per-game prefix directories."""
        results = []

        pp_dir = Path.home() / "PortProton" / "prefix"
        if not pp_dir.is_dir():
            return results

        try:
            for prefix_dir in pp_dir.iterdir():
                if not prefix_dir.is_dir():
                    continue

                if not self._is_valid_prefix(prefix_dir):
                    continue

                results.append(WinePrefix(
                    prefix_path=prefix_dir,
                    wine_binary=None,
                    source="portproton",
                    source_version="unknown",
                    store_name="unknown",
                    store_app_id=prefix_dir.name,
                    arch=self._detect_prefix_arch(prefix_dir),
                    last_used=self._get_prefix_mtime(prefix_dir),
                ))
        except OSError:
            pass

        return results

    # === FAUGUS LAUNCHER ===

    def _scan_faugus(self) -> List[WinePrefix]:
        """Scan Faugus Launcher per-game prefixes."""
        results = []

        faugus_dir = Path.home() / "Faugus"
        if not faugus_dir.is_dir():
            return results

        try:
            for prefix_dir in faugus_dir.iterdir():
                if not prefix_dir.is_dir():
                    continue

                if not self._is_valid_prefix(prefix_dir):
                    continue

                results.append(WinePrefix(
                    prefix_path=prefix_dir,
                    wine_binary=None,
                    source="faugus",
                    source_version="unknown",
                    store_name="unknown",
                    store_app_id=prefix_dir.name,
                    arch=self._detect_prefix_arch(prefix_dir),
                    last_used=self._get_prefix_mtime(prefix_dir),
                ))
        except OSError:
            pass

        return results

    # === GAMEHUB ===

    def _scan_gamehub(self) -> List[WinePrefix]:
        """Scan GameHub per-game compat directories."""
        results = []

        # GameHub stores prefixes within game directories
        # Pattern: $game_folder/_gamehub/compat/$wine_ver/
        gamehub_data = Path.home() / ".local" / "share" / "gamehub"
        if not gamehub_data.is_dir():
            return results

        try:
            for game_dir in gamehub_data.iterdir():
                if not game_dir.is_dir():
                    continue

                compat_dir = game_dir / "_gamehub" / "compat"
                if not compat_dir.is_dir():
                    continue

                for wine_ver_dir in compat_dir.iterdir():
                    if not wine_ver_dir.is_dir():
                        continue

                    if not self._is_valid_prefix(wine_ver_dir):
                        continue

                    results.append(WinePrefix(
                        prefix_path=wine_ver_dir,
                        wine_binary=None,
                        source="gamehub",
                        source_version=wine_ver_dir.name,
                        store_name="unknown",
                        store_app_id=game_dir.name,
                        arch=self._detect_prefix_arch(wine_ver_dir),
                        last_used=self._get_prefix_mtime(wine_ver_dir),
                    ))
        except OSError:
            pass

        return results

    # === GENERIC ===

    def _scan_generic(self) -> List[WinePrefix]:
        """Scan generic Wine prefix locations."""
        results = []

        generic_paths = [
            Path.home() / ".wine",
        ]

        # Also scan ~/Games for manually created prefixes
        games_dir = Path.home() / "Games"
        if games_dir.is_dir():
            try:
                for d in games_dir.iterdir():
                    if d.is_dir() and self._is_valid_prefix(d):
                        generic_paths.append(d)
            except OSError:
                pass

        for prefix_path in generic_paths:
            if not self._is_valid_prefix(prefix_path):
                continue

            results.append(WinePrefix(
                prefix_path=prefix_path,
                wine_binary=None,
                source="generic",
                source_version="unknown",
                store_name="unknown",
                store_app_id=prefix_path.name,
                arch=self._detect_prefix_arch(prefix_path),
                last_used=self._get_prefix_mtime(prefix_path),
            ))

        return results

    # === HELPERS ===

    @staticmethod
    def _is_valid_prefix(path: Path) -> bool:
        """Check if a directory looks like a valid Wine prefix.

        Required: drive_c/ directory exists.
        Cross-check: at least one of system.reg, user.reg, userdef.reg present.
        """
        if not path.is_dir():
            return False

        drive_c = path / "drive_c"
        if not drive_c.is_dir():
            return False

        # At least one registry file should exist
        for reg_file in ("system.reg", "user.reg", "userdef.reg"):
            if (path / reg_file).is_file():
                return True

        return False

    @staticmethod
    def _detect_prefix_arch(prefix_path: Path) -> str:
        """Detect if prefix is win32 or win64."""
        # Check for 64-bit marker
        syswow64 = prefix_path / "drive_c" / "windows" / "syswow64"
        if syswow64.is_dir():
            return "win64"

        # Check system.reg for arch hint
        system_reg = prefix_path / "system.reg"
        if system_reg.is_file():
            try:
                # First few KB are enough for the arch marker
                header = system_reg.read_text(errors="ignore")[:4096]
                if "#arch=win64" in header:
                    return "win64"
                if "#arch=win32" in header:
                    return "win32"
            except OSError:
                pass

        return "win64"  # Default assumption for modern prefixes

    @staticmethod
    def _get_prefix_mtime(prefix_path: Path) -> Optional[datetime]:
        """Get the last modification time of a prefix."""
        try:
            # Use drive_c mtime as proxy for last use
            drive_c = prefix_path / "drive_c"
            if drive_c.is_dir():
                return datetime.fromtimestamp(drive_c.stat().st_mtime)
        except OSError:
            pass
        return None
