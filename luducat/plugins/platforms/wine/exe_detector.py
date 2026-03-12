# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# exe_detector.py

"""Game Executable Detection

Scans Wine prefixes for game executables with confidence scoring.
Deterministic sources (GOG/Epic manifests, user saved) score >= 90
for auto-launch. Heuristic detection is capped at 89, requiring
user confirmation via the exe selection dialog.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Executables matching these patterns are excluded from detection
_EXCLUSION_PATTERNS = re.compile(
    r"(?i)^("
    r"vc_redist.*|vcredist.*|"
    r"unins\d*|uninstall.*|"
    r"unitycrashhandler.*|unitylicensingclient.*|"
    r"dxsetup|dxwebsetup|"
    r"winemenubuilder|"
    r"dotnet.*setup.*|"
    r"directx.*setup.*|"
    r"installerdata.*|"
    r"crashreport.*|bugreport.*|"
    r"updater|update|patcher|"
    r"7z.*|winrar.*|"
    r"steam_api.*|steamclient.*|"
    r"ue4prereqsetup.*|"
    r"launch-installer.*"
    r")\.exe$"
)


@dataclass
class ExeCandidate:
    """Detected game executable with confidence score."""
    path: Path
    score: int       # 0–100
    source: str      # "user_saved", "gog_manifest", "epic_manifest", "heuristic"
    reason: str      # Human-readable for tooltip


def detect_game_executables(
    prefix,
    game_title: str,
    launch_config: Optional[Dict] = None,
) -> List[ExeCandidate]:
    """Detect game executables within a Wine prefix.

    Args:
        prefix: WinePrefix object with prefix_path
        game_title: Game title for heuristic matching
        launch_config: Saved per-game launch config (may contain exe override)

    Returns:
        List of ExeCandidate sorted by score descending
    """
    candidates: List[ExeCandidate] = []
    seen_paths: set = set()

    drive_c = prefix.prefix_path / "drive_c"
    if not drive_c.is_dir():
        return candidates

    def _add(candidate: ExeCandidate) -> None:
        key = str(candidate.path.resolve())
        if key not in seen_paths:
            seen_paths.add(key)
            candidates.append(candidate)

    # 1. User saved exe (score 100)
    if launch_config:
        saved_exe = launch_config.get("wine_exe")
        if saved_exe:
            saved_path = Path(saved_exe)
            if saved_path.is_file():
                _add(ExeCandidate(
                    path=saved_path, score=100,
                    source="user_saved", reason="Previously selected by user",
                ))

    # 2. GOG manifest detection (score 95)
    _detect_gog_manifest(drive_c, prefix, _add)

    # 3. Epic manifest detection (score 95)
    _detect_epic_manifest(drive_c, prefix, _add)

    # 4. Heuristic detection (score capped at 89)
    _detect_heuristic(drive_c, game_title, _add)

    # Sort by score descending, then path for stability
    candidates.sort(key=lambda c: (-c.score, str(c.path)))

    # Boost single-candidate heuristic to 90
    if len(candidates) == 1 and candidates[0].source == "heuristic":
        candidates[0].score = 90
        candidates[0].reason += " (only candidate)"

    return candidates


def _detect_gog_manifest(
    drive_c: Path, prefix, add_fn
) -> None:
    """Parse GOG game info files for launch executables."""
    gog_games = drive_c / "GOG Games"

    # Also search Program Files for GOG installs
    search_dirs = [gog_games]
    for pf in ("Program Files", "Program Files (x86)"):
        pf_dir = drive_c / pf
        if pf_dir.is_dir():
            search_dirs.append(pf_dir)

    for search_dir in search_dirs:
        if not search_dir.is_dir():
            continue

        try:
            for info_file in search_dir.rglob("goggame-*.info"):
                _parse_gog_info(info_file, add_fn)
        except OSError:
            continue


def _parse_gog_info(info_file: Path, add_fn) -> None:
    """Parse a single GOG game info file."""
    import json

    try:
        data = json.loads(info_file.read_text(errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return

    play_tasks = data.get("playTasks")
    if not play_tasks or not isinstance(play_tasks, list):
        return

    install_dir = info_file.parent

    for task in play_tasks:
        if not isinstance(task, dict):
            continue
        exe_path = task.get("path")
        if not exe_path:
            continue

        # Resolve relative to install directory
        full_path = install_dir / exe_path
        if full_path.is_file() and full_path.suffix.lower() == ".exe":
            task_name = task.get("name", "")
            is_primary = task.get("isPrimary", False)
            score = 95 if is_primary else 85
            add_fn(ExeCandidate(
                path=full_path, score=score,
                source="gog_manifest",
                reason=f"GOG manifest: {task_name}" if task_name else "GOG manifest",
            ))


def _detect_epic_manifest(
    drive_c: Path, prefix, add_fn
) -> None:
    """Parse Epic .item manifest files for launch executables."""
    manifest_dirs = [
        drive_c / "ProgramData" / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests",
        drive_c / "ProgramData" / "Epic" / "UnrealEngineLauncher" / "Data" / "Manifests",
    ]

    for manifest_dir in manifest_dirs:
        if not manifest_dir.is_dir():
            continue

        try:
            for item_file in manifest_dir.glob("*.item"):
                _parse_epic_item(item_file, drive_c, add_fn)
        except OSError:
            continue


def _parse_epic_item(item_file: Path, drive_c: Path, add_fn) -> None:
    """Parse a single Epic .item manifest file."""
    import json

    try:
        data = json.loads(item_file.read_text(errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return

    launch_exe = data.get("LaunchExecutable")
    install_location = data.get("InstallLocation")
    if not launch_exe or not install_location:
        return

    # Install location inside the prefix
    install_path = Path(install_location)
    if not install_path.is_absolute():
        install_path = drive_c / install_location

    full_path = install_path / launch_exe
    if full_path.is_file() and full_path.suffix.lower() == ".exe":
        display_name = data.get("DisplayName", "")
        add_fn(ExeCandidate(
            path=full_path, score=95,
            source="epic_manifest",
            reason=f"Epic manifest: {display_name}" if display_name else "Epic manifest",
        ))


def _detect_heuristic(
    drive_c: Path, game_title: str, add_fn
) -> None:
    """Heuristic exe detection. Score capped at 89."""
    search_dirs = []
    for dirname in ("Program Files", "Program Files (x86)", "GOG Games"):
        d = drive_c / dirname
        if d.is_dir():
            search_dirs.append(d)

    # Normalize title for matching
    title_words = _normalize_title(game_title)

    all_exes: List[tuple] = []  # (path, file_size)

    for search_dir in search_dirs:
        try:
            for exe_path in search_dir.rglob("*.exe"):
                if _EXCLUSION_PATTERNS.match(exe_path.name):
                    continue
                try:
                    size = exe_path.stat().st_size
                except OSError:
                    continue
                all_exes.append((exe_path, size))
        except OSError:
            continue

    if not all_exes:
        return

    # Find the largest exe for comparison
    max_size = max(size for _, size in all_exes)

    for exe_path, size in all_exes:
        score = 10  # baseline
        reasons = []

        # Title match
        exe_parts = _normalize_title(exe_path.stem)
        if title_words and exe_parts:
            overlap = title_words & exe_parts
            if overlap:
                score += 30
                reasons.append("title match")

        # Top-level position (directly in an install dir, not deeply nested)
        depth = len(exe_path.relative_to(drive_c).parts)
        if depth <= 3:  # e.g. Program Files/GameName/game.exe
            score += 15
            reasons.append("top-level")

        # File size checks
        if size > 10 * 1024 * 1024:  # > 10 MB
            score += 10
            reasons.append(f"large ({size // (1024 * 1024)} MB)")

        if max_size > 0 and size == max_size:
            score += 10
            reasons.append("largest in search")

        # Cap heuristic at 89
        score = min(score, 89)

        add_fn(ExeCandidate(
            path=exe_path, score=score,
            source="heuristic",
            reason=", ".join(reasons) if reasons else "heuristic match",
        ))


def _normalize_title(title: str) -> set:
    """Normalize a title into lowercase word tokens for matching."""
    # Remove non-alphanumeric, split into words
    words = re.sub(r"[^a-zA-Z0-9\s]", " ", title.lower()).split()
    # Filter very short words
    return {w for w in words if len(w) > 2}
