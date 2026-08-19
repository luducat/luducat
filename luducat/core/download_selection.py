# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Default file selection for downloads -- shared by dialog and auditor.

The installer picker dialog pre-selects files matching the user's
``downloads.preferred_os`` / ``downloads.preferred_languages`` defaults.
The archive auditor applies the same filter to decide which store files
"should" be in the archive. Both go through this module so the two
selections cannot drift apart.

Moved from ``luducat/ui/dialogs/installer_picker.py`` (auto-selection
helpers) and ``luducat/core/download_handlers/gog.py`` (size parsing).
"""

from __future__ import annotations

import platform as _platform
import re
from typing import Any, Optional

from luducat.core.archivist.types import GameDownloadInfo

_LANG_NAME_TO_CODE: dict[str, str] = {
    "english": "en", "german": "de", "deutsch": "de",
    "french": "fr", "français": "fr", "francais": "fr",
    "spanish": "es", "español": "es", "espanol": "es",
    "italian": "it", "italiano": "it",
    "portuguese": "pt", "brazilian": "br",
    "russian": "ru", "русский": "ru",
    "polish": "pl", "polski": "pl",
    "japanese": "ja", "chinese": "zh",
    "korean": "ko", "dutch": "nl", "czech": "cs",
    "hungarian": "hu", "romanian": "ro", "turkish": "tr",
    "arabic": "ar", "thai": "th", "swedish": "sv",
    "norwegian": "no", "danish": "da", "finnish": "fi",
}

_LANG_CODES = {
    "en", "de", "fr", "es", "it", "pt", "br", "ru", "pl", "ja", "jp",
    "zh", "ko", "nl", "cs", "hu", "ro", "tr", "ar", "th", "sv", "no",
    "da", "fi",
}

_LANG_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(
        list(_LANG_NAME_TO_CODE.keys()) + list(_LANG_CODES),
        key=len, reverse=True,
    )) + r")\b",
    re.IGNORECASE,
)


def _detect_extra_language(name: str) -> Optional[str]:
    """Detect language from an extra's display name.

    Returns ISO code if a language is found, None if language-neutral.
    """
    m = _LANG_WORD_RE.search(name.lower())
    if not m:
        return None
    token = m.group(1).lower()
    if token in _LANG_NAME_TO_CODE:
        return _LANG_NAME_TO_CODE[token]
    if token in _LANG_CODES:
        if token == "jp":
            return "ja"
        return token
    return None


def _detect_platform() -> str:
    system = _platform.system()
    if system == "Linux":
        return "linux"
    elif system == "Darwin":
        return "mac"
    return "windows"


def normalize_preferred_os(preferred_os: list) -> list[str]:
    """Normalize configured OS names to the platform keys stores use.

    Older settings versions saved "macos"; store items and platform
    detection say "mac". Without this, a mac preference never matches.
    """
    return ["mac" if os_name == "macos" else os_name
            for os_name in (preferred_os or [])]


def _auto_select_indices(
    rows: list[dict],
    preferred_os: Optional[list[str]] = None,
    preferred_languages: Optional[list[str]] = None,
    include_patches: bool = True,
) -> tuple[list[int], bool]:
    """Pre-select rows matching OS/language preferences + all extras/patches.

    Returns (selected_indices, had_fallback).
    had_fallback is True if no installer matched the preferred OS.
    Extras with a detected language are only selected if the language
    matches the user's preferences; language-neutral extras are always
    selected. Patch rows are skipped entirely when the user disabled
    patch downloads (downloads.download_patches).
    """
    if preferred_os is None:
        preferred_os = [_detect_platform()]
    if preferred_languages is None:
        preferred_languages = ["all"]

    all_lang = "all" in preferred_languages

    installer_sections = {"installers", "dlc"}

    os_match = [
        i for i, r in enumerate(rows)
        if r["section"] in installer_sections
        and r["item"].get("platform") in preferred_os
    ]

    if os_match and not all_lang:
        lang_match = [
            i for i in os_match
            if not rows[i]["item"].get("language")
            or rows[i]["item"].get("language", "").lower() in preferred_languages
        ]
        if lang_match:
            os_match = lang_match

    all_installers = [
        i for i, r in enumerate(rows) if r["section"] in installer_sections
    ]

    non_installers = []
    for i, r in enumerate(rows):
        if r["section"] in installer_sections:
            continue
        if r["section"] == "patches" and not include_patches:
            continue
        if all_lang:
            non_installers.append(i)
            continue
        detected = _detect_extra_language(r["label"])
        if detected is None or detected in preferred_languages:
            non_installers.append(i)

    if os_match:
        return os_match + non_installers, False
    return all_installers + non_installers, True


def select_default_files(
    info: GameDownloadInfo,
    preferred_os: list[str],
    preferred_languages: list[str],
) -> list[dict[str, Any]]:
    """Installer items matching the user's default OS/language filter.

    Mirrors the picker's auto-selection: platform priority order with
    fallback to all platforms when none match, language filter with
    "all" wildcard. Extras and patches are never in the default set.
    """
    preferred_os = normalize_preferred_os(preferred_os)
    rows = [
        {
            "section": "dlc" if item.get("dlc_title") else "installers",
            "label": item.get("name", ""),
            "item": item,
            "platform": item.get("platform"),
        }
        for item in info.installers
    ]
    selected, _had_fallback = _auto_select_indices(
        rows, preferred_os=preferred_os, preferred_languages=preferred_languages)
    return [rows[i]["item"] for i in selected]


def _parse_size_bytes(size_str: str) -> int:
    """Best-effort parse of a human-readable size string to bytes."""
    if not size_str:
        return 0
    s = size_str.strip().upper()
    multipliers = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for suffix, mult in multipliers.items():
        if s.endswith(suffix):
            try:
                return int(float(s[:-len(suffix)].strip()) * mult)
            except ValueError:
                return 0
    try:
        return int(s)
    except ValueError:
        return 0


_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}


def _parse_size_string(s: str) -> Optional[int]:
    """Parse GOG human-readable sizes ("15 MB", "1.2 GB") into bytes."""
    if not s:
        return None
    s = s.strip()
    parts = s.split()
    if len(parts) != 2:
        return None
    try:
        value = float(parts[0])
    except ValueError:
        return None
    unit = parts[1].lower()
    multiplier = _SIZE_UNITS.get(unit)
    if multiplier is None:
        return None
    return int(value * multiplier)
