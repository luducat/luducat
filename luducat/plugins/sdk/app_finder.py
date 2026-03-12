# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# app_finder.py

"""Centralized Application Search — SDK Module

Cross-platform utility for finding installed applications. Used by runner
plugins to detect launcher installations without duplicating platform-specific
search logic across each plugin.

Self-contained: zero core imports, stdlib + pathlib only.
Read-only: no filesystem mutations.

Usage:
    from luducat.plugins.sdk.app_finder import find_application, find_wine_binary

    results = find_application(
        ["heroic"],
        flatpak_ids=["com.heroicgameslauncher.hgl"],
    )
    for r in results:
        print(f"{r.name_hint} ({r.install_type}) at {r.path}")
"""

import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Subprocess timeout for external tools (Flatpak list, xdg-mime, mdfind)
_SUBPROCESS_TIMEOUT = 5


# =============================================================================
# Data Types
# =============================================================================

@dataclass
class AppSearchResult:
    """Result of an application search.

    Attributes:
        path: Absolute path to the executable. None for Flatpak apps
              (launched via ``flatpak run``).
        install_type: How the application was installed.
            ``"system"`` — system package (in PATH or known dirs),
            ``"appimage"`` — AppImage binary,
            ``"flatpak"`` — Flatpak sandbox,
            ``"bundle"`` — macOS .app bundle,
            ``"registry"`` — Windows registry entry,
            ``"custom"`` — user-configured path.
        virtualized: True if the app runs inside a sandbox (Flatpak,
            or AppImage with ``.home`` sidecar directory).
        name_hint: Human-readable label describing what was found
            (e.g. ``"heroic (system)"``, ``"Heroic AppImage"``).
        flatpak_id: Reverse-DNS Flatpak application ID, if applicable.
        version: Detected version string, if available.
        url_handler: Registered URL scheme handler (e.g.
            ``"heroic-handler.desktop"``), if queried and found.
        sandbox_info: Extra sandbox metadata (Flatpak permissions, etc.).
    """
    path: Optional[Path]
    install_type: str
    virtualized: bool
    name_hint: str
    flatpak_id: Optional[str] = None
    version: Optional[str] = None
    url_handler: Optional[str] = None
    sandbox_info: Optional[dict] = None


# =============================================================================
# Finder Registry
# =============================================================================

_finders: Dict[str, Callable] = {}


def register_finder(name: str, finder_func: Callable) -> None:
    """Register a custom application finder.

    Custom finders are called by :func:`find_application` after all
    built-in search strategies. This allows runner plugins to contribute
    additional detection logic without modifying core code.

    Args:
        name: Unique identifier for the finder.
        finder_func: Callable accepting the same keyword arguments as
            :func:`find_application` and returning ``list[AppSearchResult]``.
    """
    _finders[name] = finder_func
    logger.debug("Registered custom finder: %s", name)


# =============================================================================
# Public API
# =============================================================================

def find_application(
    name_hints: list,
    *,
    platform_name: Optional[str] = None,
    extra_search_dirs: Optional[List[Path]] = None,
    flatpak_ids: Optional[List[str]] = None,
    include_url_handler: bool = False,
) -> List[AppSearchResult]:
    """Find installed instances of an application.

    Searches the system using platform-appropriate strategies (PATH lookup,
    known install directories, .desktop files, AppImage scan, Flatpak,
    Windows registry, macOS bundles/Spotlight).

    Results are de-duplicated by resolved path. The order is stable:
    custom path → system binary → AppImage → Flatpak → registry →
    macOS bundle → URL handler → custom finders.

    Args:
        name_hints: List of binary names or partial names to search for
            (case-insensitive matching for AppImage/bundle scans).
        platform_name: Override platform detection (``"Linux"``,
            ``"Windows"``, ``"Darwin"``). Defaults to ``platform.system()``.
        extra_search_dirs: Additional directories to search for binaries.
        flatpak_ids: Flatpak application IDs to check
            (e.g. ``["com.heroicgameslauncher.hgl"]``).
        include_url_handler: If True, also query URL handler registrations
            for each name hint.

    Returns:
        List of :class:`AppSearchResult` sorted by detection priority.
    """
    system = platform_name or platform.system()
    results: List[AppSearchResult] = []
    seen_paths: set = set()

    def _add(r: AppSearchResult) -> None:
        """De-duplicate by resolved path."""
        key = str(r.path.resolve()) if r.path else f"flatpak:{r.flatpak_id}"
        if key not in seen_paths:
            seen_paths.add(key)
            results.append(r)

    # --- Extra / user-configured search dirs ---
    if extra_search_dirs:
        for search_dir in extra_search_dirs:
            for hint in name_hints:
                _search_custom_dir(search_dir, hint, _add)

    # --- Platform-specific search ---
    if system == "Linux":
        _find_linux(name_hints, flatpak_ids, include_url_handler, _add)
    elif system == "Windows":
        _find_windows(name_hints, _add)
    elif system == "Darwin":
        _find_macos(name_hints, _add)

    # --- Custom registered finders ---
    for finder_name, finder_func in _finders.items():
        try:
            custom_results = finder_func(
                name_hints=name_hints,
                platform_name=system,
                extra_search_dirs=extra_search_dirs,
                flatpak_ids=flatpak_ids,
            )
            for r in (custom_results or []):
                _add(r)
        except Exception as e:
            logger.debug("Custom finder '%s' failed: %s", finder_name, e)

    return results


def find_wine_binary(
    extra_search_dirs: Optional[List[Path]] = None,
) -> List[AppSearchResult]:
    """Find Wine binary installations.

    Searches common Wine locations across platforms. Useful for runner
    plugins and the Wine platform provider's stale-binary resolution.

    Args:
        extra_search_dirs: Additional directories to include in search.

    Returns:
        List of :class:`AppSearchResult` for each Wine binary found.
    """
    system = platform.system()
    results: List[AppSearchResult] = []
    seen: set = set()

    def _add(r: AppSearchResult) -> None:
        key = str(r.path.resolve()) if r.path else r.name_hint
        if key not in seen:
            seen.add(key)
            results.append(r)

    # System wine
    wine_path = shutil.which("wine")
    if wine_path:
        p = Path(wine_path)
        _add(AppSearchResult(
            path=p, install_type="system", virtualized=False,
            name_hint="wine (system)",
        ))

    if system == "Linux":
        # Wine-GE / GE-Proton
        ge_dirs = [
            Path.home() / ".local" / "share" / "wine-ge",
            Path.home() / ".local" / "share" / "Steam" / "compatibilitytools.d",
            Path.home() / ".steam" / "steam" / "compatibilitytools.d",
        ]
        for d in ge_dirs:
            if not d.is_dir():
                continue
            try:
                for sub in sorted(d.iterdir(), reverse=True):
                    wine_bin = sub / "bin" / "wine"
                    if wine_bin.is_file():
                        _add(AppSearchResult(
                            path=wine_bin, install_type="system",
                            virtualized=False,
                            name_hint=f"wine ({sub.name})",
                            version=sub.name,
                        ))
            except OSError:
                continue

        # Lutris Wine runners
        lutris_runners = Path.home() / ".local" / "share" / "lutris" / "runners" / "wine"
        if lutris_runners.is_dir():
            try:
                for sub in sorted(lutris_runners.iterdir(), reverse=True):
                    wine_bin = sub / "bin" / "wine"
                    if wine_bin.is_file():
                        _add(AppSearchResult(
                            path=wine_bin, install_type="system",
                            virtualized=False,
                            name_hint=f"wine (Lutris: {sub.name})",
                            version=sub.name,
                        ))
            except OSError:
                pass

        # Bottles runners
        bottles_runners = Path.home() / ".local" / "share" / "bottles" / "runners"
        if bottles_runners.is_dir():
            try:
                for sub in sorted(bottles_runners.iterdir(), reverse=True):
                    wine_bin = sub / "bin" / "wine"
                    if wine_bin.is_file():
                        _add(AppSearchResult(
                            path=wine_bin, install_type="system",
                            virtualized=False,
                            name_hint=f"wine (Bottles: {sub.name})",
                            version=sub.name,
                        ))
            except OSError:
                pass

    # Extra dirs
    if extra_search_dirs:
        for d in extra_search_dirs:
            if not d.is_dir():
                continue
            wine_bin = d / "bin" / "wine"
            if wine_bin.is_file():
                _add(AppSearchResult(
                    path=wine_bin, install_type="custom",
                    virtualized=False,
                    name_hint=f"wine ({d.name})",
                ))
            # Also check directly in dir
            for name in ("wine", "wine64"):
                p = d / name
                if p.is_file():
                    _add(AppSearchResult(
                        path=p, install_type="custom",
                        virtualized=False,
                        name_hint=f"{name} ({d})",
                    ))

    return results


def find_url_handler(scheme: str) -> Optional[str]:
    """Query the system for a URL scheme handler.

    Args:
        scheme: URL scheme without ``://`` (e.g. ``"heroic"``, ``"steam"``).

    Returns:
        Handler identifier (e.g. desktop file name on Linux, protocol
        command on Windows), or None if not registered.
    """
    system = platform.system()

    if system == "Linux":
        return _query_xdg_mime_handler(scheme)
    elif system == "Windows":
        return _query_windows_url_handler(scheme)
    elif system == "Darwin":
        return _query_macos_url_handler(scheme)

    return None


# =============================================================================
# Linux Search
# =============================================================================

def _find_linux(
    name_hints: list,
    flatpak_ids: Optional[List[str]],
    include_url_handler: bool,
    add: Callable,
) -> None:
    """Linux-specific application search."""
    for hint in name_hints:
        # System binary via PATH
        which_result = shutil.which(hint)
        if which_result:
            p = Path(which_result)
            add(AppSearchResult(
                path=p, install_type="system", virtualized=False,
                name_hint=f"{hint} (system)",
            ))

        # Known system paths (not in PATH on some distros)
        for sys_dir in ("/usr/bin", "/usr/local/bin", "/usr/games"):
            candidate = Path(sys_dir) / hint
            if candidate.is_file():
                add(AppSearchResult(
                    path=candidate, install_type="system",
                    virtualized=False,
                    name_hint=f"{hint} ({sys_dir})",
                ))

    # AppImage scan
    _find_appimages(name_hints, add)

    # Flatpak
    if flatpak_ids:
        _find_flatpak(flatpak_ids, add)

    # URL handler query
    if include_url_handler:
        for hint in name_hints:
            handler = _query_xdg_mime_handler(hint)
            if handler:
                add(AppSearchResult(
                    path=None, install_type="system", virtualized=False,
                    name_hint=f"{hint} (URL handler)",
                    url_handler=handler,
                ))


def _find_appimages(name_hints: list, add: Callable) -> None:
    """Scan common directories for AppImage files matching name hints."""
    home = Path.home()
    search_dirs = [
        home / "Applications",
        home / "Downloads",
        home / ".local" / "bin",
        home / "bin",
        home,
    ]

    candidates: List[tuple] = []  # (path, hint)

    for search_dir in search_dirs:
        if not search_dir.is_dir():
            continue
        try:
            for p in search_dir.iterdir():
                name_lower = p.name.lower()
                if not name_lower.endswith(".appimage"):
                    continue
                for hint in name_hints:
                    if hint.lower() in name_lower:
                        candidates.append((p, hint))
                        break
        except OSError:
            continue

    # Filter to executable files, sort by mtime (newest first)
    valid: List[tuple] = []
    for p, hint in candidates:
        try:
            st = p.stat()
            if p.is_file() and (st.st_mode & 0o111) != 0:
                valid.append((p, hint, st.st_mtime))
        except OSError:
            continue

    valid.sort(key=lambda x: x[2], reverse=True)

    for p, hint, _mtime in valid:
        # Check for .home sidecar (sandboxed AppImage)
        home_sidecar = p.parent / (p.stem + ".home")
        virtualized = home_sidecar.is_dir()

        add(AppSearchResult(
            path=p, install_type="appimage", virtualized=virtualized,
            name_hint=f"{hint} AppImage ({p.name})",
        ))


def _find_flatpak(flatpak_ids: List[str], add: Callable) -> None:
    """Check for installed Flatpak applications."""
    # Quick check: does the Flatpak data directory exist?
    for fid in flatpak_ids:
        flatpak_dir = Path.home() / ".var" / "app" / fid
        if flatpak_dir.exists():
            add(AppSearchResult(
                path=None, install_type="flatpak", virtualized=True,
                name_hint=f"{fid} (Flatpak)",
                flatpak_id=fid,
                sandbox_info={"data_dir": str(flatpak_dir)},
            ))
            continue

        # If no data dir, try flatpak list (heavier)
        try:
            result = subprocess.run(
                ["flatpak", "list", "--app", "--columns=application"],
                capture_output=True, text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
            if result.returncode == 0 and fid in result.stdout:
                add(AppSearchResult(
                    path=None, install_type="flatpak", virtualized=True,
                    name_hint=f"{fid} (Flatpak)",
                    flatpak_id=fid,
                ))
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass


def _query_xdg_mime_handler(scheme: str) -> Optional[str]:
    """Query xdg-mime for a URL scheme handler on Linux."""
    try:
        result = subprocess.run(
            ["xdg-mime", "query", "default", f"x-scheme-handler/{scheme}"],
            capture_output=True, text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        handler = result.stdout.strip()
        if handler:
            return handler
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


# =============================================================================
# Windows Search
# =============================================================================

def _find_windows(name_hints: list, add: Callable) -> None:
    """Windows-specific application search."""
    for hint in name_hints:
        # PATH lookup
        which_result = shutil.which(hint)
        if which_result:
            add(AppSearchResult(
                path=Path(which_result), install_type="system",
                virtualized=False,
                name_hint=f"{hint} (system)",
            ))

    # Known install paths
    _find_windows_known_paths(name_hints, add)

    # Registry search
    _find_windows_registry(name_hints, add)


def _find_windows_known_paths(name_hints: list, add: Callable) -> None:
    """Search common Windows installation directories."""
    env_dirs = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        val = os.environ.get(env_var)
        if val:
            env_dirs.append(Path(val))

    for hint in name_hints:
        hint_lower = hint.lower()
        # Try direct exe name
        exe_names = [f"{hint}.exe", f"{hint.title()}.exe"]

        for base_dir in env_dirs:
            if not base_dir.is_dir():
                continue
            # Check Programs subdirectory (common for LOCALAPPDATA installs)
            search_bases = [base_dir, base_dir / "Programs"]
            for sb in search_bases:
                if not sb.is_dir():
                    continue
                try:
                    for subdir in sb.iterdir():
                        if not subdir.is_dir():
                            continue
                        if hint_lower not in subdir.name.lower():
                            continue
                        for exe_name in exe_names:
                            exe = subdir / exe_name
                            if exe.is_file():
                                add(AppSearchResult(
                                    path=exe, install_type="system",
                                    virtualized=False,
                                    name_hint=f"{hint} ({subdir.name})",
                                ))
                except OSError:
                    continue


def _find_windows_registry(name_hints: list, add: Callable) -> None:
    """Search Windows registry Uninstall keys and protocol handlers."""
    try:
        import winreg
    except ImportError:
        return

    # Search Uninstall keys
    uninstall_roots = [
        (winreg.HKEY_CURRENT_USER,
         r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    for hint in name_hints:
        hint_lower = hint.lower()

        for root, subkey in uninstall_roots:
            try:
                with winreg.OpenKey(root, subkey) as uninstall:
                    num_subkeys, _, _ = winreg.QueryInfoKey(uninstall)
                    for i in range(num_subkeys):
                        try:
                            sk_name = winreg.EnumKey(uninstall, i)
                            with winreg.OpenKey(uninstall, sk_name) as appkey:
                                try:
                                    display_name, _ = winreg.QueryValueEx(
                                        appkey, "DisplayName"
                                    )
                                except OSError:
                                    continue

                                if hint_lower not in str(display_name).lower():
                                    continue

                                # Try InstallLocation then DisplayIcon
                                for value_name in ("InstallLocation", "DisplayIcon"):
                                    try:
                                        val, _ = winreg.QueryValueEx(
                                            appkey, value_name
                                        )
                                    except OSError:
                                        continue

                                    exe = Path(val)
                                    if exe.is_dir():
                                        # Try common exe names
                                        for ename in (f"{hint}.exe", f"{hint.title()}.exe"):
                                            candidate = exe / ename
                                            if candidate.is_file():
                                                exe = candidate
                                                break
                                    if exe.is_file():
                                        version = None
                                        try:
                                            version, _ = winreg.QueryValueEx(
                                                appkey, "DisplayVersion"
                                            )
                                        except OSError:
                                            pass
                                        add(AppSearchResult(
                                            path=exe,
                                            install_type="registry",
                                            virtualized=False,
                                            name_hint=f"{display_name}",
                                            version=version,
                                        ))
                                        break  # Found exe, stop trying value names
                        except OSError:
                            continue
            except OSError:
                continue

    # Protocol handler search
    for hint in name_hints:
        handler_exe = _query_windows_protocol_handler(hint)
        if handler_exe:
            add(AppSearchResult(
                path=handler_exe, install_type="registry",
                virtualized=False,
                name_hint=f"{hint} (protocol handler)",
                url_handler=f"{hint}://",
            ))


def _query_windows_protocol_handler(scheme: str) -> Optional[Path]:
    """Query Windows registry for a URL protocol handler."""
    try:
        import winreg
    except ImportError:
        return None

    try:
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT,
            rf"{scheme}\shell\open\command"
        ) as key:
            cmd, _ = winreg.QueryValueEx(key, None)
    except OSError:
        return None

    cmd_str = str(cmd).strip()
    exe_path: Optional[str] = None

    if cmd_str.startswith('"'):
        closing = cmd_str.find('"', 1)
        if closing > 1:
            exe_path = cmd_str[1:closing]
    else:
        exe_path = cmd_str.split(" ", 1)[0]

    if exe_path:
        p = Path(exe_path)
        if p.is_file():
            return p
    return None


def _query_windows_url_handler(scheme: str) -> Optional[str]:
    """Query Windows for URL handler (returns command string)."""
    try:
        import winreg
    except ImportError:
        return None

    try:
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT,
            rf"{scheme}\shell\open\command"
        ) as key:
            cmd, _ = winreg.QueryValueEx(key, None)
            return str(cmd).strip() if cmd else None
    except OSError:
        return None


# =============================================================================
# macOS Search
# =============================================================================

def _find_macos(name_hints: list, add: Callable) -> None:
    """macOS-specific application search."""
    for hint in name_hints:
        # PATH lookup
        which_result = shutil.which(hint)
        if which_result:
            add(AppSearchResult(
                path=Path(which_result), install_type="system",
                virtualized=False,
                name_hint=f"{hint} (system)",
            ))

    # /Applications bundles
    _find_macos_bundles(name_hints, add)

    # Spotlight search
    _find_macos_spotlight(name_hints, add)


def _find_macos_bundles(name_hints: list, add: Callable) -> None:
    """Search /Applications for .app bundles matching name hints."""
    app_dirs = [
        Path("/Applications"),
        Path.home() / "Applications",
    ]

    for hint in name_hints:
        hint_lower = hint.lower()
        for app_dir in app_dirs:
            if not app_dir.is_dir():
                continue
            try:
                for entry in app_dir.iterdir():
                    if not entry.name.endswith(".app"):
                        continue
                    if hint_lower in entry.name.lower():
                        add(AppSearchResult(
                            path=entry, install_type="bundle",
                            virtualized=False,
                            name_hint=f"{entry.stem} ({app_dir})",
                        ))
            except OSError:
                continue


def _find_macos_spotlight(name_hints: list, add: Callable) -> None:
    """Use Spotlight (mdfind) to locate applications on macOS."""
    for hint in name_hints:
        try:
            result = subprocess.run(
                [
                    "mdfind",
                    f'kMDItemKind == "Application" && kMDItemDisplayName == "*{hint}*"',
                ],
                capture_output=True, text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    p = Path(line.strip())
                    if p.exists():
                        add(AppSearchResult(
                            path=p, install_type="bundle",
                            virtualized=False,
                            name_hint=f"{p.stem} (Spotlight)",
                        ))
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass


def _query_macos_url_handler(scheme: str) -> Optional[str]:
    """Query macOS for a URL scheme handler using AppleScript."""
    # This is best-effort; Launch Services is hard to query from CLI
    try:
        # Use plutil/defaults to check URL handlers, but there's no clean CLI
        # for this. Returning None is acceptable — macOS URL handlers work
        # at the OS level and don't need pre-verification.
        pass
    except Exception:
        pass
    return None


# =============================================================================
# Common Helpers
# =============================================================================

def _search_custom_dir(search_dir: Path, hint: str, add: Callable) -> None:
    """Search a specific directory for a binary matching hint."""
    if not search_dir.is_dir():
        return

    hint_lower = hint.lower()

    # Direct binary match
    for name in (hint, hint.lower(), hint.title()):
        candidate = search_dir / name
        if candidate.is_file():
            add(AppSearchResult(
                path=candidate, install_type="custom",
                virtualized=False,
                name_hint=f"{hint} ({search_dir})",
            ))
            return

    # Windows .exe variant
    if platform.system() == "Windows":
        for name in (f"{hint}.exe", f"{hint.title()}.exe"):
            candidate = search_dir / name
            if candidate.is_file():
                add(AppSearchResult(
                    path=candidate, install_type="custom",
                    virtualized=False,
                    name_hint=f"{hint} ({search_dir})",
                ))
                return

    # Subdirectory scan (one level deep)
    try:
        for subdir in search_dir.iterdir():
            if not subdir.is_dir():
                continue
            if hint_lower not in subdir.name.lower():
                continue
            # Check for executable inside matching subdir
            for name in (hint, hint.lower(), hint.title()):
                candidate = subdir / name
                if candidate.is_file():
                    add(AppSearchResult(
                        path=candidate, install_type="custom",
                        virtualized=False,
                        name_hint=f"{hint} ({subdir})",
                    ))
                    return
            if platform.system() == "Windows":
                for name in (f"{hint}.exe", f"{hint.title()}.exe"):
                    candidate = subdir / name
                    if candidate.is_file():
                        add(AppSearchResult(
                            path=candidate, install_type="custom",
                            virtualized=False,
                            name_hint=f"{hint} ({subdir})",
                        ))
                        return
    except OSError:
        pass
