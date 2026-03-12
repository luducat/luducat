# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# runner_resolver.py

"""Wine/Proton Binary Resolution

Resolves the correct Wine or Proton binary for launching a game in a
given prefix. Follows a priority chain: prefix hint → user override →
Proton compatibility tools → system Wine.
"""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ResolvedRunner:
    """Result of Wine/Proton binary resolution."""
    wine_binary: Path
    is_proton: bool
    proton_path: Optional[Path]
    umu_run: Optional[Path]
    version: str
    source: str  # "prefix_hint", "user_setting", "proton_compat", "system_wine"


def is_proton_runner(path: Path) -> bool:
    """Check if a path points to a Proton installation.

    Detects Proton by looking for the ``proton`` script or
    ``dist/bin/wine`` / ``files/bin/wine`` layout.
    """
    if not path.exists():
        return False

    # Direct proton script
    if path.name == "proton" and path.is_file():
        return True

    # Directory containing proton script
    if path.is_dir():
        if (path / "proton").is_file():
            return True

    # Wine binary inside Proton dist layout
    parent = path.parent
    if parent.name == "bin":
        grandparent = parent.parent
        if grandparent.name in ("dist", "files"):
            proton_root = grandparent.parent
            if (proton_root / "proton").is_file():
                return True

    return False


def get_wine_from_proton(proton_path: Path) -> Optional[Path]:
    """Extract the Wine binary from a Proton installation.

    Checks ``dist/bin/wine`` (Proton 7+) and ``files/bin/wine``
    (older Proton).
    """
    if not proton_path.is_dir():
        return None

    for subdir in ("dist", "files"):
        wine_bin = proton_path / subdir / "bin" / "wine"
        if wine_bin.is_file():
            return wine_bin

    return None


def find_umu_run() -> Optional[Path]:
    """Find the umu-run launcher.

    Checks PATH first, then Heroic's bundled location.
    """
    # System PATH
    which = shutil.which("umu-run")
    if which:
        return Path(which)

    # Heroic bundled location
    heroic_umu = (
        Path.home() / ".local" / "share" / "heroic" / "tools" / "umu" / "umu-run"
    )
    if heroic_umu.is_file():
        return heroic_umu

    return None


def find_proton_for_prefix(version_str: str) -> Optional[Path]:
    """Find a Proton installation matching a version string.

    Scans Steam's ``compatibilitytools.d`` and the default Proton
    directories for a matching version.
    """
    if not version_str or version_str == "unknown":
        return None

    version_lower = version_str.lower()

    compat_dirs = [
        Path.home() / ".local" / "share" / "Steam" / "compatibilitytools.d",
        Path.home() / ".steam" / "steam" / "compatibilitytools.d",
    ]

    for compat_dir in compat_dirs:
        if not compat_dir.is_dir():
            continue
        try:
            for entry in compat_dir.iterdir():
                if not entry.is_dir():
                    continue
                if version_lower in entry.name.lower():
                    if (entry / "proton").is_file():
                        return entry
        except OSError:
            continue

    return None


class RunnerResolver:
    """Resolves Wine/Proton binary for a given prefix."""

    def resolve_specific(self, runtime) -> Optional[ResolvedRunner]:
        """Resolve using a specific user-selected InstalledRuntime.

        Args:
            runtime: InstalledRuntime from RuntimeScanner
        """
        if runtime.runtime_type == "umu":
            # umu-run needs a Proton to pair with — find any
            proton_dir = self._find_any_proton()
            wine_bin = None
            if proton_dir:
                wine_bin = get_wine_from_proton(proton_dir)
            return ResolvedRunner(
                wine_binary=wine_bin or runtime.wine_binary,
                is_proton=True,
                proton_path=proton_dir,
                umu_run=runtime.wine_binary,
                version=runtime.version,
                source="user_setting",
            )

        is_proton = runtime.runtime_type == "proton"
        proton_path = runtime.path if is_proton else None
        umu_run = find_umu_run() if is_proton else None

        return ResolvedRunner(
            wine_binary=runtime.wine_binary,
            is_proton=is_proton,
            proton_path=proton_path,
            umu_run=umu_run,
            version=runtime.version,
            source="user_setting",
        )

    def resolve(
        self,
        prefix,
        user_wine_binary: Optional[Path] = None,
        runtime_mode: str = "auto",
        user_proton_directory: Optional[Path] = None,
        user_umu_binary: Optional[Path] = None,
    ) -> Optional[ResolvedRunner]:
        """Resolve the best Wine/Proton binary for a prefix.

        Args:
            prefix: WinePrefix with prefix_path, wine_binary, source_version
            user_wine_binary: Per-game or settings Wine binary override
            runtime_mode: "auto", "wine", "proton", or "umu"
            user_proton_directory: Settings Proton directory override
            user_umu_binary: Settings umu-run binary override

        When runtime_mode is "auto", follows the priority chain:
        1. prefix.wine_binary (from PrefixDetector)
        2. user_wine_binary (per-game override)
        3. Proton from compatibilitytools.d matching prefix version
        4. System Wine via app_finder
        5. None → RuntimeManager falls back to runner plugin

        When runtime_mode is forced, resolves that specific type.
        """
        if runtime_mode == "wine":
            return self._resolve_wine(user_wine_binary)
        elif runtime_mode == "proton":
            return self._resolve_proton(user_proton_directory)
        elif runtime_mode == "umu":
            return self._resolve_umu(user_umu_binary, user_proton_directory)

        # auto mode — existing priority chain
        return self._resolve_auto(prefix, user_wine_binary)

    def _resolve_auto(
        self, prefix, user_wine_binary: Optional[Path],
    ) -> Optional[ResolvedRunner]:
        """Auto-detect the best runner (original priority chain)."""
        # 1. Prefix hint
        if prefix.wine_binary and prefix.wine_binary.is_file():
            proton = is_proton_runner(prefix.wine_binary)
            proton_path = None
            if proton:
                proton_path = _find_proton_root(prefix.wine_binary)
            return ResolvedRunner(
                wine_binary=prefix.wine_binary,
                is_proton=proton,
                proton_path=proton_path,
                umu_run=find_umu_run() if proton else None,
                version=prefix.source_version,
                source="prefix_hint",
            )

        # 2. User override
        if user_wine_binary and user_wine_binary.is_file():
            proton = is_proton_runner(user_wine_binary)
            proton_path = None
            if proton:
                proton_path = _find_proton_root(user_wine_binary)
            return ResolvedRunner(
                wine_binary=user_wine_binary,
                is_proton=proton,
                proton_path=proton_path,
                umu_run=find_umu_run() if proton else None,
                version="user",
                source="user_setting",
            )

        # 3. Proton from compatibilitytools.d
        proton_dir = find_proton_for_prefix(prefix.source_version)
        if proton_dir:
            wine_bin = get_wine_from_proton(proton_dir)
            if wine_bin:
                return ResolvedRunner(
                    wine_binary=wine_bin,
                    is_proton=True,
                    proton_path=proton_dir,
                    umu_run=find_umu_run(),
                    version=proton_dir.name,
                    source="proton_compat",
                )

        # 4. System Wine
        return self._find_system_wine()

    def _resolve_wine(
        self, user_binary: Optional[Path],
    ) -> Optional[ResolvedRunner]:
        """Force Wine mode."""
        if user_binary and user_binary.is_file():
            return ResolvedRunner(
                wine_binary=user_binary,
                is_proton=False,
                proton_path=None,
                umu_run=None,
                version="user",
                source="user_setting",
            )
        return self._find_system_wine()

    def _resolve_proton(
        self, user_proton_dir: Optional[Path],
    ) -> Optional[ResolvedRunner]:
        """Force Proton mode."""
        proton_dir = None
        if user_proton_dir and user_proton_dir.is_dir():
            proton_dir = user_proton_dir
        else:
            # Scan compatibilitytools.d for any Proton
            proton_dir = self._find_any_proton()

        if proton_dir:
            wine_bin = get_wine_from_proton(proton_dir)
            if wine_bin:
                return ResolvedRunner(
                    wine_binary=wine_bin,
                    is_proton=True,
                    proton_path=proton_dir,
                    umu_run=find_umu_run(),
                    version=proton_dir.name,
                    source="user_setting" if user_proton_dir else "proton_compat",
                )
        return None

    def _resolve_umu(
        self,
        user_umu_binary: Optional[Path],
        user_proton_dir: Optional[Path],
    ) -> Optional[ResolvedRunner]:
        """Force umu-run mode."""
        umu_bin = None
        if user_umu_binary and user_umu_binary.is_file():
            umu_bin = user_umu_binary
        else:
            umu_bin = find_umu_run()

        if not umu_bin:
            return None

        # Need a Proton installation for umu-run
        proton_dir = None
        if user_proton_dir and user_proton_dir.is_dir():
            proton_dir = user_proton_dir
        else:
            proton_dir = self._find_any_proton()

        wine_bin = None
        if proton_dir:
            wine_bin = get_wine_from_proton(proton_dir)

        return ResolvedRunner(
            wine_binary=wine_bin or umu_bin,
            is_proton=True,
            proton_path=proton_dir,
            umu_run=umu_bin,
            version=proton_dir.name if proton_dir else "umu",
            source="user_setting",
        )

    def _find_system_wine(self) -> Optional[ResolvedRunner]:
        """Find system Wine via app_finder."""
        try:
            from luducat.plugins.sdk.app_finder import find_wine_binary
            results = find_wine_binary()
            if results:
                first = results[0]
                if first.path and first.path.is_file():
                    return ResolvedRunner(
                        wine_binary=first.path,
                        is_proton=False,
                        proton_path=None,
                        umu_run=None,
                        version=first.version or "system",
                        source="system_wine",
                    )
        except Exception:
            logger.debug("app_finder.find_wine_binary() failed", exc_info=True)
        return None

    @staticmethod
    def _find_any_proton() -> Optional[Path]:
        """Find any available Proton across all known tool directories."""
        home = Path.home()
        search_dirs = [
            # Steam custom
            home / ".local" / "share" / "Steam" / "compatibilitytools.d",
            home / ".steam" / "steam" / "compatibilitytools.d",
            # Steam Flatpak
            home / ".var" / "app" / "com.valvesoftware.Steam" / "data"
            / "Steam" / "compatibilitytools.d",
            # Heroic proton
            home / ".config" / "heroic" / "tools" / "proton",
            home / ".var" / "app" / "com.heroicgameslauncher.hgl" / "config"
            / "heroic" / "tools" / "proton",
            # Lutris wine (may contain Proton-type builds)
            home / ".local" / "share" / "lutris" / "runners" / "wine",
            # Bottles runners
            home / ".local" / "share" / "bottles" / "runners",
        ]
        for search_dir in search_dirs:
            if not search_dir.is_dir():
                continue
            try:
                for entry in sorted(search_dir.iterdir(), reverse=True):
                    if entry.is_dir() and (entry / "proton").is_file():
                        return entry
            except OSError:
                continue
        return None


def _find_proton_root(wine_path: Path) -> Optional[Path]:
    """Walk up from a Wine binary to find the Proton root directory."""
    # wine_path might be: proton_dir/dist/bin/wine or proton_dir/files/bin/wine
    current = wine_path
    for _level in range(4):
        current = current.parent
        if (current / "proton").is_file():
            return current
    return None
