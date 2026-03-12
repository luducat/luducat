# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# runtime_scanner.py

"""Wine/Proton Runtime Scanner

Scans ALL Wine/Proton installations across the system — Steam, Heroic,
Lutris, Bottles, system packages. Returns a unified list of InstalledRuntime
objects with consistent naming.

Sources scanned (native + Flatpak):
- Steam: compatibilitytools.d (custom), steamapps/common/Proton* (built-in)
- Heroic: tools/wine/, tools/proton/
- Lutris: runners/wine/
- Bottles: runners/
- System: wine/wine64 via PATH
- umu-run: PATH + Heroic bundled
"""

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class InstalledRuntime:
    """A detected Wine/Proton installation."""
    name: str            # Display name: "Proton 9.0-4", "Wine-GE-8-26"
    source: str          # "steam", "heroic", "lutris", "bottles", "system"
    runtime_type: str    # "proton", "wine", "umu"
    version: str         # Version string
    path: Path           # Root directory or binary path
    wine_binary: Path    # Actual wine binary inside the installation

    @property
    def identifier(self) -> str:
        """Unique identifier for config storage, e.g. 'steam/GE-Proton9-20'."""
        return f"{self.source}/{self.path.name}"

    @property
    def display_label(self) -> str:
        """Formatted label for UI dropdowns."""
        source_labels = {
            "steam": "Steam",
            "heroic": "Heroic",
            "lutris": "Lutris",
            "bottles": "Bottles",
            "system": "System",
        }
        src = source_labels.get(self.source, self.source.title())
        return f"({src}) {self.name}"


# ── Sort algorithm ───────────────────────────────────────────────────────

def _source_rank(rt: InstalledRuntime) -> int:
    """Rank by installation source. Lower = preferred."""
    if rt.source == "system" and rt.runtime_type == "wine":
        return 0
    if rt.source == "steam":
        return 1
    if rt.runtime_type == "umu":
        return 2
    return {"lutris": 3, "heroic": 4, "bottles": 5,
            "minigalaxy": 6, "faugus": 7}.get(rt.source, 99)


def _version_tier(rt: InstalledRuntime) -> int:
    """Rank by version tier. Lower = preferred."""
    name_lower = rt.name.lower()
    if "hotfix" in name_lower:
        return 0
    if "experimental" in name_lower:
        return 1
    if name_lower.endswith("-latest") or rt.version == "latest":
        return 2
    return 3


def _parse_version_numbers(name: str) -> Tuple[int, ...]:
    """Extract version numbers from a runtime name."""
    nums = re.findall(r'\d+', name)
    return tuple(int(n) for n in nums) if nums else (0,)


def _variant_rank(name: str) -> int:
    """Rank by runtime variant. Lower = preferred."""
    # Check compound patterns first
    if "GE-Proton" in name or "Proton-GE" in name:
        return 2
    if "Wine-GE" in name:
        return 3
    if name.startswith("Proton"):
        return 0
    if name.startswith("Wine") or name.startswith("wine"):
        return 1
    return 4


def _runtime_sort_key(rt: InstalledRuntime) -> tuple:
    """Composite sort key for runtime ordering."""
    ver = _parse_version_numbers(rt.name)
    return (
        _source_rank(rt),
        _version_tier(rt),
        tuple(-v for v in ver) if ver else (0,),
        _variant_rank(rt.name),
        rt.name.lower(),
    )


def sort_runtimes(runtimes: List[InstalledRuntime]) -> List[InstalledRuntime]:
    """Return a new list of runtimes sorted by recommendation quality."""
    return sorted(runtimes, key=_runtime_sort_key)


# ── Directory definitions ────────────────────────────────────────────────

def _steam_roots() -> List[Path]:
    """All possible Steam root directories (native + Flatpak + Snap)."""
    home = Path.home()
    return [
        home / ".local" / "share" / "Steam",
        home / ".steam" / "root",
        home / ".steam" / "steam",
        home / ".steam" / "debian-installation",
        # Flatpak
        home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
        # Snap
        home / "snap" / "steam" / "common" / ".steam" / "root",
    ]


def _heroic_tool_dirs() -> List[dict]:
    """Heroic tool directories (native + Flatpak), with type annotation."""
    home = Path.home()
    bases = [
        home / ".config" / "heroic" / "tools",
        home / ".var" / "app" / "com.heroicgameslauncher.hgl" / "config"
        / "heroic" / "tools",
    ]
    results = []
    for base in bases:
        results.append({"path": base / "wine", "runtime_type": "wine"})
        results.append({"path": base / "proton", "runtime_type": "proton"})
    return results


def _lutris_dirs() -> List[Path]:
    """Lutris wine runner directories (native + Flatpak)."""
    home = Path.home()
    return [
        home / ".local" / "share" / "lutris" / "runners" / "wine",
        home / ".var" / "app" / "net.lutris.Lutris" / "data"
        / "lutris" / "runners" / "wine",
    ]


def _bottles_dirs() -> List[Path]:
    """Bottles runner directories (native + Flatpak)."""
    home = Path.home()
    return [
        home / ".local" / "share" / "bottles" / "runners",
        home / ".var" / "app" / "com.usebottles.bottles" / "data"
        / "bottles" / "runners",
    ]


# ── Version detection ────────────────────────────────────────────────────

def _read_version_txt(tool_dir: Path) -> Optional[str]:
    """Read VERSION.txt from a tool directory."""
    vfile = tool_dir / "VERSION.txt"
    if vfile.is_file():
        try:
            return vfile.read_text().strip()
        except OSError:
            pass
    return None


def _read_compat_vdf_name(tool_dir: Path) -> Optional[str]:
    """Read display_name or internal_name from compatibilitytool.vdf."""
    vdf_path = tool_dir / "compatibilitytool.vdf"
    if not vdf_path.is_file():
        return None
    try:
        text = vdf_path.read_text(errors="replace")
        # Look for "display_name" first, then "internal_name"
        for key in ("display_name", "internal_name"):
            match = re.search(rf'"{key}"\s+"([^"]+)"', text)
            if match:
                return match.group(1)
    except OSError:
        pass
    return None


def _find_wine_binary_in(tool_dir: Path) -> Optional[Path]:
    """Find the wine binary inside a tool directory."""
    # Proton layout: dist/bin/wine or files/bin/wine
    for subdir in ("dist", "files"):
        wine = tool_dir / subdir / "bin" / "wine"
        if wine.is_file():
            return wine
    # Direct Wine layout: bin/wine
    wine = tool_dir / "bin" / "wine"
    if wine.is_file():
        return wine
    # Lutris/Bottles: sometimes wine binary is at lutris-*/bin/wine or similar
    # but the tool_dir IS the wine build root — try bin/wine64 too
    wine64 = tool_dir / "bin" / "wine64"
    if wine64.is_file():
        return wine64
    return None


def _is_proton_dir(tool_dir: Path) -> bool:
    """Check if a directory is a Proton installation."""
    return (tool_dir / "proton").is_file()


def _derive_name(tool_dir: Path, vdf_name: Optional[str],
                 version_txt: Optional[str]) -> str:
    """Derive a display name from available metadata."""
    if vdf_name:
        return vdf_name
    if version_txt:
        return version_txt
    return tool_dir.name


# ── Scanner functions ────────────────────────────────────────────────────

def _scan_steam_compat_tools(seen_paths: set) -> List[InstalledRuntime]:
    """Scan Steam compatibilitytools.d for custom Proton/Wine builds."""
    runtimes = []
    for root in _steam_roots():
        compat_dir = root / "compatibilitytools.d"
        if not compat_dir.is_dir():
            continue
        try:
            for entry in sorted(compat_dir.iterdir()):
                if not entry.is_dir() or entry.resolve() in seen_paths:
                    continue
                seen_paths.add(entry.resolve())

                wine_bin = _find_wine_binary_in(entry)
                if not wine_bin:
                    continue

                is_proton = _is_proton_dir(entry)
                vdf_name = _read_compat_vdf_name(entry)
                version_txt = _read_version_txt(entry)
                name = _derive_name(entry, vdf_name, version_txt)

                runtimes.append(InstalledRuntime(
                    name=name,
                    source="steam",
                    runtime_type="proton" if is_proton else "wine",
                    version=version_txt or entry.name,
                    path=entry,
                    wine_binary=wine_bin,
                ))
        except OSError:
            continue
    return runtimes


def _scan_steam_builtin_proton(seen_paths: set) -> List[InstalledRuntime]:
    """Scan Steam's built-in Proton installations (steamapps/common/Proton*)."""
    runtimes = []
    for root in _steam_roots():
        common_dir = root / "steamapps" / "common"
        if not common_dir.is_dir():
            continue
        try:
            for entry in sorted(common_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if not entry.name.lower().startswith("proton"):
                    continue
                if entry.resolve() in seen_paths:
                    continue
                seen_paths.add(entry.resolve())

                wine_bin = _find_wine_binary_in(entry)
                if not wine_bin:
                    continue
                if not _is_proton_dir(entry):
                    continue

                version_txt = _read_version_txt(entry)
                name = _derive_name(entry, None, version_txt)

                runtimes.append(InstalledRuntime(
                    name=name,
                    source="steam",
                    runtime_type="proton",
                    version=version_txt or entry.name,
                    path=entry,
                    wine_binary=wine_bin,
                ))
        except OSError:
            continue
    return runtimes


def _scan_heroic(seen_paths: set) -> List[InstalledRuntime]:
    """Scan Heroic wine and proton tool directories."""
    runtimes = []
    for info in _heroic_tool_dirs():
        tool_dir = info["path"]
        rt_type = info["runtime_type"]
        if not tool_dir.is_dir():
            continue
        try:
            for entry in sorted(tool_dir.iterdir()):
                if not entry.is_dir() or entry.resolve() in seen_paths:
                    continue
                seen_paths.add(entry.resolve())

                wine_bin = _find_wine_binary_in(entry)
                if not wine_bin:
                    continue

                # Heroic Proton dirs also have proton script
                actual_type = rt_type
                if _is_proton_dir(entry):
                    actual_type = "proton"

                version_txt = _read_version_txt(entry)
                name = _derive_name(entry, None, version_txt)

                runtimes.append(InstalledRuntime(
                    name=name,
                    source="heroic",
                    runtime_type=actual_type,
                    version=version_txt or entry.name,
                    path=entry,
                    wine_binary=wine_bin,
                ))
        except OSError:
            continue
    return runtimes


def _scan_lutris(seen_paths: set) -> List[InstalledRuntime]:
    """Scan Lutris wine runner directories."""
    runtimes = []
    for lutris_dir in _lutris_dirs():
        if not lutris_dir.is_dir():
            continue
        try:
            for entry in sorted(lutris_dir.iterdir()):
                if not entry.is_dir() or entry.resolve() in seen_paths:
                    continue
                seen_paths.add(entry.resolve())

                wine_bin = _find_wine_binary_in(entry)
                if not wine_bin:
                    continue

                actual_type = "proton" if _is_proton_dir(entry) else "wine"
                version_txt = _read_version_txt(entry)
                name = _derive_name(entry, None, version_txt)

                runtimes.append(InstalledRuntime(
                    name=name,
                    source="lutris",
                    runtime_type=actual_type,
                    version=version_txt or entry.name,
                    path=entry,
                    wine_binary=wine_bin,
                ))
        except OSError:
            continue
    return runtimes


def _scan_bottles(seen_paths: set) -> List[InstalledRuntime]:
    """Scan Bottles runner directories."""
    runtimes = []
    for bottles_dir in _bottles_dirs():
        if not bottles_dir.is_dir():
            continue
        try:
            for entry in sorted(bottles_dir.iterdir()):
                if not entry.is_dir() or entry.resolve() in seen_paths:
                    continue
                seen_paths.add(entry.resolve())

                wine_bin = _find_wine_binary_in(entry)
                if not wine_bin:
                    continue

                actual_type = "proton" if _is_proton_dir(entry) else "wine"
                version_txt = _read_version_txt(entry)
                name = _derive_name(entry, None, version_txt)

                runtimes.append(InstalledRuntime(
                    name=name,
                    source="bottles",
                    runtime_type=actual_type,
                    version=version_txt or entry.name,
                    path=entry,
                    wine_binary=wine_bin,
                ))
        except OSError:
            continue
    return runtimes


def _detect_wine_version(wine_path: Path) -> Optional[str]:
    """Run wine --version and parse the output (e.g. 'wine-10.0')."""
    try:
        result = subprocess.run(
            [str(wine_path), "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _scan_system(seen_paths: set) -> List[InstalledRuntime]:
    """Detect system Wine and umu-run."""
    runtimes = []

    # System Wine — prefer 'wine' binary, check wine64 for arch detection
    has_wine64 = shutil.which("wine64") is not None
    wine_which = shutil.which("wine") or shutil.which("wine64")

    if wine_which:
        wine_path = Path(wine_which)
        resolved = wine_path.resolve()
        if resolved not in seen_paths:
            seen_paths.add(resolved)

            version_str = _detect_wine_version(wine_path)
            # Parse: "wine-10.0 (Debian 10.0~repack-6)" → "10.0"
            version = "system"
            if version_str:
                m = re.match(r'wine-(\S+)', version_str)
                if m:
                    version = m.group(1)

            arch = "x64" if has_wine64 else "x32"
            display = f"Wine {version} ({arch})"

            runtimes.append(InstalledRuntime(
                name=display,
                source="system",
                runtime_type="wine",
                version=version,
                path=wine_path,
                wine_binary=wine_path,
            ))

    # umu-run
    from .runner_resolver import find_umu_run
    umu_path = find_umu_run()
    if umu_path:
        resolved = umu_path.resolve()
        if resolved not in seen_paths:
            seen_paths.add(resolved)
            runtimes.append(InstalledRuntime(
                name="umu-run",
                source="system",
                runtime_type="umu",
                version="system",
                path=umu_path,
                wine_binary=umu_path,
            ))

    return runtimes


# ── Public API ───────────────────────────────────────────────────────────

def scan_installed_runtimes() -> List[InstalledRuntime]:
    """Scan all sources for installed Wine/Proton runtimes.

    Deduplicates by resolved path. Returns sorted by source then name.
    """
    seen_paths: set = set()
    runtimes: List[InstalledRuntime] = []

    runtimes.extend(_scan_steam_compat_tools(seen_paths))
    runtimes.extend(_scan_steam_builtin_proton(seen_paths))
    runtimes.extend(_scan_heroic(seen_paths))
    runtimes.extend(_scan_lutris(seen_paths))
    runtimes.extend(_scan_bottles(seen_paths))
    runtimes.extend(_scan_system(seen_paths))

    logger.info(
        "RuntimeScanner: found %d runtime(s) across %d source(s)",
        len(runtimes),
        len({r.source for r in runtimes}),
    )
    return sort_runtimes(runtimes)


def find_runtime_by_identifier(
    identifier: str,
    runtimes: Optional[List[InstalledRuntime]] = None,
) -> Optional[InstalledRuntime]:
    """Find a runtime by its identifier string (e.g. 'steam/GE-Proton9-20').

    If runtimes is None, performs a fresh scan.
    """
    if runtimes is None:
        runtimes = scan_installed_runtimes()
    for rt in runtimes:
        if rt.identifier == identifier:
            return rt
    return None
