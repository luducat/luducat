# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Installer picker dialog -- lets the user choose which files to download.

Shown when a game has multiple installer options (platforms, languages)
or extras (soundtracks, manuals, etc.).
"""

from __future__ import annotations

import os
import platform as _platform
import re
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from luducat.core.archivist.types import GameDownloadInfo

try:
    _("")
except NameError:
    def _(s): return s


# -- Part type heuristics --

_PART_TYPE_MAP = {
    ".exe": "setup executable",
    ".bin": "setup data",
    ".sh": "setup executable",
    ".pkg": "setup executable",
    ".sit": "setup executable",
    ".hqx": "setup executable",
    ".dmg": "system package",
    ".deb": "system package",
    ".rpm": "system package",
    ".zip": "archive",
}


def _guess_part_type(item: dict) -> str:
    """Infer part type from filename extension."""
    name = item.get("name", "")
    downlink = item.get("downlink", "")
    for source in (name, downlink.rsplit("/", 1)[-1] if downlink else ""):
        if not source:
            continue
        dot = source.rfind(".")
        if dot >= 0:
            ext = source[dot:].lower()
            if ext in _PART_TYPE_MAP:
                return _PART_TYPE_MAP[ext]
    return "installer"


# -- Data helpers (tested without Qt) --


def _build_picker_rows(info: GameDownloadInfo) -> list[dict]:
    """Build flat list of picker rows from a GameDownloadInfo.

    Each row: {section, label, sublabel, item, platform}
    """
    rows: list[dict] = []

    for item in info.installers:
        plat = item.get("platform", "unknown")
        size = item.get("size", "")
        lang = item.get("language", "")
        version = item.get("version", "")
        name = item.get("name", info.game_title)

        label_parts = [name]
        if version:
            label_parts.append(version)
        label_parts.append(f"({plat.capitalize()})")
        if lang:
            label_parts.append(f"[{lang.upper()}]")
        label = " ".join(label_parts)

        part_type = _guess_part_type(item)
        sublabel_parts = [part_type]
        if size:
            sublabel_parts.append(size)
        sublabel = " - ".join(sublabel_parts)

        section = "dlc" if item.get("dlc_title") else "installers"

        rows.append({
            "section": section,
            "label": label,
            "sublabel": sublabel,
            "item": item,
            "platform": plat,
        })

    for item in info.patches:
        plat = item.get("platform", "unknown")
        name = item.get("name", "Patch")
        size = item.get("size", "")
        version = item.get("version", "")

        label_parts = [name]
        if version:
            label_parts.append(version)
        label_parts.append(f"({plat.capitalize()})")
        label = " ".join(label_parts)

        part_type = _guess_part_type(item)
        sublabel_parts = [part_type]
        if size:
            sublabel_parts.append(size)

        rows.append({
            "section": "patches",
            "label": label,
            "sublabel": " - ".join(sublabel_parts),
            "item": item,
            "platform": plat,
        })

    for item in info.extras:
        name = item.get("name", "Extra")
        size = item.get("size", "")
        etype = item.get("type", "")

        sublabel_parts = []
        if etype:
            sublabel_parts.append(etype)
        if size:
            sublabel_parts.append(size)

        rows.append({
            "section": "extras",
            "label": name,
            "sublabel": " - ".join(sublabel_parts) if sublabel_parts else "",
            "item": item,
            "platform": None,
        })

    _SECTION_ORDER = {"installers": 0, "dlc": 1, "patches": 2, "extras": 3}
    rows.sort(key=lambda r: _SECTION_ORDER.get(r["section"], 99))
    return rows


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


def _auto_select_indices(
    rows: list[dict],
    preferred_os: Optional[list[str]] = None,
    preferred_languages: Optional[list[str]] = None,
) -> tuple[list[int], bool]:
    """Pre-select rows matching OS/language preferences + all extras/patches.

    Returns (selected_indices, had_fallback).
    had_fallback is True if no installer matched the preferred OS.
    Extras with a detected language are only selected if the language
    matches the user's preferences; language-neutral extras are always selected.
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
        if all_lang:
            non_installers.append(i)
            continue
        detected = _detect_extra_language(r["label"])
        if detected is None or detected in preferred_languages:
            non_installers.append(i)

    if os_match:
        return os_match + non_installers, False
    return all_installers + non_installers, True


def _detect_platform() -> str:
    system = _platform.system()
    if system == "Linux":
        return "linux"
    elif system == "Darwin":
        return "mac"
    return "windows"


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


# -- Dialog --


class InstallerPickerDialog(QDialog):
    """Modal dialog for selecting which files to download for a game.

    Usage:
        dialog = InstallerPickerDialog(info, parent)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = dialog.selected_items()
            target = handler.resolve_downloads(info, selected)
    """

    def __init__(
        self,
        info: GameDownloadInfo,
        parent: Optional[QWidget] = None,
        config=None,
    ) -> None:
        super().__init__(parent)
        self._info = info
        self._config = config
        self._rows = _build_picker_rows(info)
        self._checkboxes: list[QCheckBox] = []
        self._ok_button: Optional[QPushButton] = None
        self._size_label: Optional[QLabel] = None
        self._space_info_label: Optional[QLabel] = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle(
            _("Download - {title}").format(title=self._info.game_title)
        )
        self.setMinimumWidth(650)
        self.setMinimumHeight(420)

        layout = QVBoxLayout(self)

        # Title
        title_label = QLabel(f"<b>{self._info.game_title}</b>")
        layout.addWidget(title_label)

        section_names = {
            "installers": _("Installers"),
            "dlc": _("DLC"),
            "patches": _("Patches"),
            "extras": _("Extras"),
        }

        # Scrollable checkbox area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setSpacing(4)

        current_section = ""
        for i, row in enumerate(self._rows):
            if row["section"] != current_section:
                current_section = row["section"]
                header_text = section_names.get(
                    current_section, current_section.capitalize())
                header = QLabel(f"<b>{header_text}</b>")
                header.setObjectName("hintLabel")
                scroll_layout.addWidget(header)

            cb_layout = QHBoxLayout()
            cb = QCheckBox(row["label"])
            cb.setToolTip(row["sublabel"])
            cb.stateChanged.connect(self._on_selection_changed)
            self._checkboxes.append(cb)
            cb_layout.addWidget(cb, 1)

            if row["sublabel"]:
                size_label = QLabel(row["sublabel"])
                size_label.setObjectName("hintLabel")
                size_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
                size_label.setMinimumWidth(120)
                cb_layout.addWidget(size_label)

            scroll_layout.addLayout(cb_layout)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll, 1)

        # Row 1: OS toggle buttons + All/None
        row1 = QHBoxLayout()
        platforms_present = sorted({
            r["platform"] for r in self._rows
            if r["section"] in ("installers", "dlc", "patches") and r["platform"]
        })
        os_names = {"windows": _("Windows"), "linux": _("Linux"), "mac": _("macOS")}
        for plat in platforms_present:
            btn = QPushButton(os_names.get(plat, plat.capitalize()))
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.clicked.connect(lambda checked, p=plat: self._toggle_platform(p, checked))
            row1.addWidget(btn)

        row1.addStretch()
        btn_all = QPushButton(_("All"))
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none = QPushButton(_("None"))
        btn_none.clicked.connect(lambda: self._set_all(False))
        row1.addWidget(btn_all)
        row1.addWidget(btn_none)
        layout.addLayout(row1)

        # Row 2: Section toggle buttons
        sections_present = sorted({r["section"] for r in self._rows})
        if len(sections_present) > 1:
            row2 = QHBoxLayout()
            for section in ("installers", "dlc", "patches", "extras"):
                if section not in sections_present:
                    continue
                btn = QPushButton(section_names.get(section, section.capitalize()))
                btn.setCheckable(True)
                btn.setChecked(True)
                btn.clicked.connect(
                    lambda checked, s=section: self._toggle_section(s, checked))
                row2.addWidget(btn)
            row2.addStretch()
            layout.addLayout(row2)

        # Row 3: Reset + size + OK/Cancel
        row3 = QHBoxLayout()
        btn_reset = QPushButton(_("Reset"))
        btn_reset.setToolTip(_("Restore default selection from preferences"))
        btn_reset.clicked.connect(self._reset_selection)
        row3.addWidget(btn_reset)

        self._size_label = QLabel("")
        self._size_label.setObjectName("hintLabel")
        self._size_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._size_label.setToolTip(
            _("Click to check disk space and show download directory"))
        self._size_label.mousePressEvent = lambda _: self._show_space_info()
        row3.addWidget(self._size_label)
        row3.addStretch()

        self._ok_button = QPushButton(_("OK"))
        self._ok_button.setDefault(True)
        self._ok_button.clicked.connect(self.accept)
        btn_cancel = QPushButton(_("Cancel"))
        btn_cancel.clicked.connect(self.reject)
        row3.addWidget(self._ok_button)
        row3.addWidget(btn_cancel)
        layout.addLayout(row3)

        # Space info (hidden until clicked)
        self._space_info_label = QLabel("")
        self._space_info_label.setObjectName("hintLabel")
        self._space_info_label.setWordWrap(True)
        self._space_info_label.hide()
        layout.addWidget(self._space_info_label)

        # Auto-select based on preferences
        pref_os, pref_lang = self._load_preferences()
        self._pref_os = pref_os
        self._pref_lang = pref_lang

        auto, had_fallback = _auto_select_indices(
            self._rows, preferred_os=pref_os, preferred_languages=pref_lang)

        if had_fallback:
            os_label = ", ".join(n.capitalize() for n in pref_os)
            warn = QLabel(
                _("No installers found for {os}. Showing all available.").format(
                    os=os_label))
            warn.setStyleSheet("color: #ff9800; padding: 4px;")
            warn.setWordWrap(True)
            layout.insertWidget(1, warn)

        for idx in auto:
            self._checkboxes[idx].setChecked(True)

        self._update_size_label()

    def _load_preferences(self) -> tuple[list[str], list[str]]:
        if self._config:
            pref_os = self._config.get("downloads.preferred_os", [_detect_platform()])
            pref_lang = self._config.get("downloads.preferred_languages", ["all"])
        else:
            pref_os = [_detect_platform()]
            pref_lang = ["all"]
        return pref_os, pref_lang

    def _on_selection_changed(self) -> None:
        self._update_size_label()

    def _update_size_label(self) -> None:
        total = 0
        for i, cb in enumerate(self._checkboxes):
            if cb.isChecked():
                total += _parse_size_bytes(
                    self._rows[i]["item"].get("size", ""))

        if total >= 1024**3:
            display = f"{total / 1024**3:.1f} GB"
        elif total >= 1024**2:
            display = f"{total / 1024**2:.0f} MB"
        elif total > 0:
            display = f"{total / 1024:.0f} KB"
        else:
            display = "0 MB"

        self._size_label.setText(_("Size: {size}").format(size=display))

        # Check free space against download directory
        ok_enabled = True
        try:
            dl_dir = self._get_download_dir()
            if dl_dir and dl_dir.exists():
                free = os.statvfs(dl_dir)
                free_bytes = free.f_bavail * free.f_frsize
                if total > free_bytes:
                    self._size_label.setStyleSheet("color: red;")
                    ok_enabled = False
                else:
                    self._size_label.setStyleSheet("")
            else:
                self._size_label.setStyleSheet("")
        except Exception:
            self._size_label.setStyleSheet("")

        if self._ok_button:
            self._ok_button.setEnabled(ok_enabled)

    def _get_download_dir(self) -> Optional[Path]:
        if self._config:
            dl_path = self._config.get("downloads.directory", "")
            if dl_path:
                return Path(dl_path)
        return None

    def _show_space_info(self) -> None:
        dl_dir = self._get_download_dir()
        if not dl_dir:
            self._space_info_label.setText(
                _("No download directory configured."))
            self._space_info_label.show()
            return

        if not dl_dir.exists():
            self._space_info_label.setText(
                _("Download directory does not exist: {path}").format(
                    path=str(dl_dir)))
            self._space_info_label.show()
            return

        try:
            stat = os.statvfs(dl_dir)
            free_bytes = stat.f_bavail * stat.f_frsize
            if free_bytes >= 1024**3:
                free_str = f"{free_bytes / 1024**3:.1f} GB"
            else:
                free_str = f"{free_bytes / 1024**2:.0f} MB"
            self._space_info_label.setText(
                _("{path} - {free} free").format(
                    path=str(dl_dir), free=free_str))
        except Exception:
            self._space_info_label.setText(str(dl_dir))

        self._space_info_label.show()

    def _toggle_platform(self, platform: str, checked: bool) -> None:
        for i, row in enumerate(self._rows):
            if row["platform"] == platform:
                self._checkboxes[i].setChecked(checked)

    def _toggle_section(self, section: str, checked: bool) -> None:
        for i, row in enumerate(self._rows):
            if row["section"] == section:
                self._checkboxes[i].setChecked(checked)

    def _set_all(self, checked: bool) -> None:
        for cb in self._checkboxes:
            cb.setChecked(checked)

    def _reset_selection(self) -> None:
        for cb in self._checkboxes:
            cb.setChecked(False)
        auto, _ = _auto_select_indices(
            self._rows, preferred_os=self._pref_os,
            preferred_languages=self._pref_lang)
        for idx in auto:
            self._checkboxes[idx].setChecked(True)

    def selected_items(self) -> list[dict]:
        """Return the original item dicts for all checked rows."""
        return [
            self._rows[i]["item"]
            for i, cb in enumerate(self._checkboxes)
            if cb.isChecked()
        ]
