# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# setup_wizard.py

"""First-run setup wizard for luducat

Guides new users through essential setup with two paths:

Full wizard:   Welcome → Consent → Theme → Stores → Credentials → Backup → Update
Quick setup:   Welcome → Consent → Stores → Credentials → Update

Quick setup applies defaults: system theme, backups enabled (~/luducat_Backups).
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from luducat.core.json_compat import json
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from ...core.config import Config
from ...core.backup_manager import get_default_backup_dir
from ...core.constants import APP_NAME
from ...core.plugin_manager import PluginManager
from ...core.theme_manager import ThemeManager, get_themes_dir
from ...plugins.base import PluginType

logger = logging.getLogger(__name__)

# ── Page ID constants for routing ────────────────────────────────────

PAGE_WELCOME = 0
PAGE_CONSENT = 1
PAGE_THEME = 2
PAGE_STORES = 3
PAGE_CREDENTIALS = 4
PAGE_BACKUP = 5
PAGE_UPDATE = 6


# ── Helper functions ─────────────────────────────────────────────────

def _get_app_icon_path() -> Optional[Path]:
    """Get path to app icon, checking multiple locations"""
    package_dir = Path(__file__).parent.parent.parent
    icon_paths = [
        package_dir / "assets" / "appicons" / "app_icon_128x128.png",
        package_dir / "assets" / "appicons" / "app_icon_64x64.png",
    ]
    for path in icon_paths:
        if path.exists():
            return path
    return None


def _load_themed_svg(svg_path: Path, size: int = 32) -> QPixmap:
    """Load an SVG and recolor it to match the current theme's text color.

    Uses QPainter composition to replace all non-transparent pixels with
    the palette WindowText color, so the icon adapts to any theme.
    """
    source = QPixmap(str(svg_path))
    if source.isNull():
        return QPixmap()
    source = source.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    color = QApplication.palette().color(QPalette.ColorRole.WindowText)
    colored = QPixmap(source.size())
    colored.fill(Qt.GlobalColor.transparent)
    painter = QPainter(colored)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
    painter.drawPixmap(0, 0, source)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(colored.rect(), color)
    painter.end()
    return colored


def _wizard_icons_dir() -> Path:
    """Return the wizard icons directory."""
    return Path(__file__).parent.parent.parent / "assets" / "wizard"


# ── Page 0: Welcome ─────────────────────────────────────────────────

class WelcomePage(QWizardPage):
    """Welcome page with app overview, language selection, and quick setup option.

    Replaces the old LanguagePage + IntroductionPage.
    """

    def __init__(self, config: Config, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._config = config

        self.setTitle(_("Welcome"))
        self.setSubTitle(_("Your games, one place"))

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── App icon + heading + blurb ──

        top_layout = QHBoxLayout()
        top_layout.setSpacing(16)

        icon_path = _get_app_icon_path()
        if icon_path:
            icon_label = QLabel()
            pixmap = QPixmap(str(icon_path))
            if not pixmap.isNull():
                pixmap = pixmap.scaled(
                    128, 128,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                icon_label.setPixmap(pixmap)
                icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)
                top_layout.addWidget(icon_label)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(8)

        self._lbl_heading = QLabel(
            "<b>" + _("Welcome to {app_name}!").format(app_name=APP_NAME) + "</b>"
        )
        text_layout.addWidget(self._lbl_heading)

        self._lbl_blurb = QLabel(
            _("Browse all your game libraries in one place \u2014 covers, ratings, "
              "compatibility data, powerful filters, tag and favorite import, "
              "offline-capable, and fully private with no telemetry.")
        )
        self._lbl_blurb.setWordWrap(True)
        text_layout.addWidget(self._lbl_blurb)

        self._lbl_approach = QLabel(
            _("Browse, organize, and explore \u2014 launching is handled by your "
              "platform's native launcher.")
        )
        self._lbl_approach.setWordWrap(True)
        text_layout.addWidget(self._lbl_approach)

        text_layout.addStretch()
        top_layout.addLayout(text_layout, 1)
        layout.addLayout(top_layout)

        layout.addSpacing(8)

        # ── Language dropdown ──

        lang_row = QHBoxLayout()
        self._lbl_lang = QLabel(_("Language:"))
        self._lbl_lang.setMinimumWidth(160)
        lang_row.addWidget(self._lbl_lang)

        self.lang_combo = QComboBox()
        self.lang_combo.setToolTip(
            _("Change the display language for the entire application")
        )

        from ...core.i18n import get_available_languages, _detect_system_language
        self._available = get_available_languages()

        for code, name in self._available.items():
            self.lang_combo.addItem(name, code)

        # Auto-detect and pre-select
        detected = _detect_system_language()
        saved = config.get("app.language", "")
        target = saved if saved else detected
        idx = self.lang_combo.findData(target)
        if idx >= 0:
            self.lang_combo.setCurrentIndex(idx)
        else:
            # Fallback to English
            idx = self.lang_combo.findData("en")
            if idx >= 0:
                self.lang_combo.setCurrentIndex(idx)

        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        lang_row.addWidget(self.lang_combo, 1)
        layout.addLayout(lang_row)

        # ── Quick Setup dropdown ──

        quick_row = QHBoxLayout()
        self._lbl_quick = QLabel(_("Quick Setup:"))
        self._lbl_quick.setMinimumWidth(160)
        quick_row.addWidget(self._lbl_quick)

        self.quick_combo = QComboBox()
        self.quick_combo.setToolTip(
            _("{app_name} pre-configures backups, a default theme, and update "
              "checks. You will still choose your game stores and enter "
              "credentials. Consent is required for reading browser cookies and "
              "local launcher data (tags, favorites, playtime) from other "
              "applications on your machine.").format(app_name=APP_NAME)
        )
        self.quick_combo.addItem(_("No, full wizard"), False)
        self.quick_combo.addItem(_("Yes, use defaults (recommended)"), True)
        quick_row.addWidget(self.quick_combo, 1)
        layout.addLayout(quick_row)

        layout.addStretch()

    def retranslateUi(self) -> None:
        """Update all translatable text after language change."""
        self.setTitle(_("Welcome"))
        self.setSubTitle(_("Your games, one place"))
        self._lbl_heading.setText(
            "<b>" + _("Welcome to {app_name}!").format(app_name=APP_NAME) + "</b>"
        )
        self._lbl_blurb.setText(
            _("Browse all your game libraries in one place \u2014 covers, ratings, "
              "compatibility data, powerful filters, tag and favorite import, "
              "offline-capable, and fully private with no telemetry.")
        )
        self._lbl_approach.setText(
            _("Browse, organize, and explore \u2014 launching is handled by your "
              "platform's native launcher.")
        )
        self._lbl_lang.setText(_("Language:"))
        self.lang_combo.setToolTip(
            _("Change the display language for the entire application")
        )
        self._lbl_quick.setText(_("Quick Setup:"))
        self.quick_combo.setToolTip(
            _("{app_name} pre-configures backups, a default theme, and update "
              "checks. You will still choose your game stores and enter "
              "credentials. Consent is required for reading browser cookies and "
              "local launcher data (tags, favorites, playtime) from other "
              "applications on your machine.").format(app_name=APP_NAME)
        )
        self.quick_combo.setItemText(0, _("No, full wizard"))
        self.quick_combo.setItemText(1, _("Yes, use defaults (recommended)"))

    def _on_language_changed(self, index: int) -> None:
        """Switch language live for preview (config is saved only on finish)."""
        code = self.lang_combo.itemData(index)
        if not code:
            return

        from ...core.i18n import init_i18n
        init_i18n(code)

        wizard = self.wizard()
        if wizard and hasattr(wizard, "_rebuild_pages_for_language"):
            wizard._rebuild_pages_for_language()

    def get_selected_language(self) -> str:
        """Return the selected language code."""
        return self.lang_combo.currentData() or "en"

    def is_quick_setup(self) -> bool:
        """Return whether Quick Setup was selected."""
        return bool(self.quick_combo.currentData())

    def nextId(self) -> int:
        return PAGE_CONSENT


# ── Page 1: Data Access Consent ──────────────────────────────────────

class DataAccessConsentPage(QWizardPage):
    """Data access consent page — mandatory gate for browser cookies
    and local launcher data access.

    Replaces the old informational PrivacyPage with an actionable consent
    checkbox that gates local data access throughout the application.
    """

    def __init__(self, config: Config, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._config = config

        self.setTitle(_("Data Access"))
        self.setSubTitle(
            _("Grant permission to read local data from your browsers and game launchers")
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── Consent checkbox ──

        self.consent_checkbox = QCheckBox(
            _("Allow access to browser cookies and local game launcher data")
        )
        self.consent_checkbox.setChecked(False)
        self.consent_checkbox.stateChanged.connect(
            lambda _unused: self.completeChanged.emit()
        )
        layout.addWidget(self.consent_checkbox)

        self._lbl_consent_desc = QLabel(
            _("{app_name} reads your browser's store login cookies for "
              "authentication and imports tags, favorites, playtime, and install "
              "status from Steam, GOG Galaxy, Heroic, and other launchers on "
              "your machine. All data stays local. You can change this anytime "
              "in Settings \u2192 Privacy.").format(app_name=APP_NAME)
        )
        self._lbl_consent_desc.setWordWrap(True)
        self._lbl_consent_desc.setObjectName("wizardFieldDescription")
        layout.addWidget(self._lbl_consent_desc)

        # ── Privacy disclosure sections ──

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        content_layout = QVBoxLayout(container)
        content_layout.setSpacing(12)
        content_layout.setContentsMargins(0, 0, 0, 0)

        icons_dir = _wizard_icons_dir()

        self._section_labels = []
        for icon_name, heading, body in self._privacy_sections():
            section = QHBoxLayout()
            section.setSpacing(12)

            icon_label = QLabel()
            icon_path = icons_dir / icon_name
            if icon_path.exists():
                icon_label.setPixmap(_load_themed_svg(icon_path))
            icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)
            icon_label.setFixedSize(32, 32)
            section.addWidget(icon_label)

            text = QLabel(f"<b>{heading}</b><br>{body}")
            text.setWordWrap(True)
            text.setTextFormat(Qt.TextFormat.RichText)
            text.setAlignment(Qt.AlignmentFlag.AlignTop)
            section.addWidget(text, 1)
            self._section_labels.append(text)

            content_layout.addLayout(section)

        content_layout.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        # ── Content filter checkbox ──

        separator2 = QFrame()
        separator2.setFrameShape(QFrame.Shape.HLine)
        separator2.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator2)

        self.chk_content_filter = QCheckBox(_("Hide adult-rated games from library"))
        self.chk_content_filter.setChecked(True)
        self.chk_content_filter.setToolTip(
            _("Uses confidence scoring from multiple sources (age ratings, content "
              "descriptors) to identify adult content. You can change this later "
              "in Settings \u2192 Advanced.")
        )
        layout.addWidget(self.chk_content_filter)

    @staticmethod
    def _privacy_sections():
        """Return (icon_name, heading, body) tuples for the privacy sections."""
        return [
            (
                "privacy_local.svg",
                _("Your data stays on your machine"),
                _("All game data, settings, and metadata are stored locally. "
                  "No accounts, no cloud, no tracking."),
            ),
            (
                "privacy_access.svg",
                _("How store connections work"),
                _("Store plugins access your game libraries via official APIs "
                  "or browser cookies for authentication. You provide API keys "
                  "yourself; cookies are read from your browser with your "
                  "consent above."),
            ),
            (
                "privacy_proxy.svg",
                _("Game info from public sources"),
                _("Game information (genres, ratings, screenshots, compatibility) "
                  "comes from public databases via a privacy-respecting proxy. "
                  "The proxy sees only game IDs, never your identity."),
            ),
        ]

    def retranslateUi(self) -> None:
        """Update all translatable text after language change."""
        self.setTitle(_("Data Access"))
        self.setSubTitle(
            _("Grant permission to read local data from your browsers and game launchers")
        )
        self.consent_checkbox.setText(
            _("Allow access to browser cookies and local game launcher data")
        )
        self._lbl_consent_desc.setText(
            _("{app_name} reads your browser's store login cookies for "
              "authentication and imports tags, favorites, playtime, and install "
              "status from Steam, GOG Galaxy, Heroic, and other launchers on "
              "your machine. All data stays local. You can change this anytime "
              "in Settings \u2192 Privacy.").format(app_name=APP_NAME)
        )
        for label, (_icon, heading, body) in zip(
            self._section_labels, self._privacy_sections()
        ):
            label.setText(f"<b>{heading}</b><br>{body}")
        self.chk_content_filter.setText(_("Hide adult-rated games from library"))
        self.chk_content_filter.setToolTip(
            _("Uses confidence scoring from multiple sources (age ratings, content "
              "descriptors) to identify adult content. You can change this later "
              "in Settings \u2192 Advanced.")
        )

    def initializePage(self) -> None:
        """Pre-fill consent checkbox from config on re-run."""
        existing = self._config.get("privacy.local_data_access_consent", False)
        if existing:
            self.consent_checkbox.setChecked(True)

        existing_filter = self._config.get("content_filter.enabled", True)
        self.chk_content_filter.setChecked(existing_filter)

    def isComplete(self) -> bool:
        """Next button is disabled until consent checkbox is checked."""
        return self.consent_checkbox.isChecked()

    def is_content_filter_enabled(self) -> bool:
        """Return whether the user opted into content filtering."""
        return self.chk_content_filter.isChecked()

    def is_consent_granted(self) -> bool:
        """Return whether data access consent was granted."""
        return self.consent_checkbox.isChecked()

    def nextId(self) -> int:
        wizard = self.wizard()
        if wizard and getattr(wizard, '_quick_setup', False):
            return PAGE_STORES
        return PAGE_THEME


# ── Page 2: Theme Selection ──────────────────────────────────────────

class ThemeSelectionPage(QWizardPage):
    """Theme selection with live preview — full wizard only."""

    def __init__(
        self,
        theme_manager: ThemeManager,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self._original_theme = theme_manager.get_current_theme()

        self.setTitle(_("Choose Your Theme"))
        self.setSubTitle(_("Select a visual theme (you can change this later in Settings)"))

        layout = QVBoxLayout(self)

        # ── Page icon ──
        icons_dir = _wizard_icons_dir()
        self._lbl_intro = None
        icon_path = icons_dir / "wizard_theme.svg"
        if icon_path.exists():
            icon_row = QHBoxLayout()
            icon_label = QLabel()
            icon_label.setPixmap(_load_themed_svg(icon_path, size=32))
            icon_label.setFixedSize(32, 32)
            icon_row.addWidget(icon_label)

            self._lbl_intro = QLabel(
                _("Preview themes live. The theme applies instantly as you select it.")
            )
            self._lbl_intro.setWordWrap(True)
            icon_row.addWidget(self._lbl_intro, 1)
            layout.addLayout(icon_row)
            layout.addSpacing(4)

        # Theme list (vertical scrollable)
        self.theme_list = QListWidget()
        self.theme_list.setObjectName("wizardThemeList")
        self.theme_list.setMinimumHeight(200)
        self.theme_list.setUniformItemSizes(True)
        self.theme_list.currentItemChanged.connect(self._on_theme_selected)
        layout.addWidget(self.theme_list)

        # Metadata display area
        self.meta_frame = QFrame()
        self.meta_frame.setObjectName("wizardThemeMeta")
        meta_layout = QVBoxLayout(self.meta_frame)
        meta_layout.setContentsMargins(8, 8, 8, 8)

        self.author_label = QLabel()
        self.author_label.setObjectName("wizardThemeAuthor")
        self.desc_label = QLabel()
        self.desc_label.setObjectName("wizardThemeDesc")
        self.desc_label.setWordWrap(True)

        meta_layout.addWidget(self.author_label)
        meta_layout.addWidget(self.desc_label)
        layout.addWidget(self.meta_frame)

        # Populate themes
        self._populate_themes()

    def retranslateUi(self) -> None:
        """Update all translatable text after language change."""
        self.setTitle(_("Choose Your Theme"))
        self.setSubTitle(
            _("Select a visual theme (you can change this later in Settings)")
        )
        if self._lbl_intro:
            self._lbl_intro.setText(
                _("Preview themes live. The theme applies instantly as you select it.")
            )

    def _populate_themes(self) -> None:
        """Populate list with all available themes (system, variant, package, legacy)"""
        available = self.theme_manager.get_available_themes()

        for theme in available:
            theme_id = theme["id"]
            name = theme["name"]
            author, description = self._get_theme_metadata(theme_id)

            item = QListWidgetItem(name)
            item.setData(
                Qt.ItemDataRole.UserRole,
                {
                    "id": theme_id,
                    "author": author,
                    "description": description,
                }
            )
            self.theme_list.addItem(item)

        # Pre-select system theme as default
        target_theme = "system"
        selected_row = 0
        for i in range(self.theme_list.count()):
            item = self.theme_list.item(i)
            meta = item.data(Qt.ItemDataRole.UserRole)
            if meta and meta.get("id") == target_theme:
                selected_row = i
                break

        self.theme_list.setCurrentRow(selected_row)

    def _get_theme_metadata(self, theme_id: str) -> tuple:
        """Extract (author, description) for a theme ID."""
        if theme_id == "system":
            return ("", _("Uses your system's default appearance"))

        themes_dir = get_themes_dir()

        if theme_id.startswith("variant:"):
            variant_name = theme_id.split(":", 1)[1]
            json_path = themes_dir / "variants" / f"{variant_name}.json"
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return (data.get("author", ""), data.get("description", ""))
            except Exception:
                return ("", "")

        if theme_id.startswith("package:"):
            pkg_name = theme_id.split(":", 1)[1]
            pkg_path = themes_dir / f"{pkg_name}.luducat-theme"
            try:
                import zipfile
                with zipfile.ZipFile(pkg_path, 'r') as zf:
                    with zf.open("theme.json") as f:
                        data = json.load(f)
                        return (data.get("author", ""), data.get("description", ""))
            except Exception:
                return ("", "")

        if theme_id.startswith("custom:"):
            qss_name = theme_id.split(":", 1)[1]
            qss_path = themes_dir / f"{qss_name}.qss"
            meta = self.theme_manager.get_theme_metadata(qss_path)
            return (meta.get("author", ""), meta.get("description", ""))

        return ("", "")

    def _on_theme_selected(self, current: QListWidgetItem, _previous: QListWidgetItem) -> None:
        """Update metadata display and apply live preview"""
        if not current:
            return

        meta = current.data(Qt.ItemDataRole.UserRole)
        if not meta:
            return

        # Update metadata labels
        if meta.get("author"):
            self.author_label.setText(_("By: {author}").format(author=meta['author']))
            self.author_label.show()
        else:
            self.author_label.hide()

        if meta.get("description"):
            self.desc_label.setText(meta["description"])
            self.desc_label.show()
        else:
            self.desc_label.hide()

        # Live preview — let theme font apply naturally
        theme_id = meta.get("id", "system")
        self.theme_manager.apply_theme(theme_id)

        # Force the wizard to repolish after theme change — QWizard's
        # internal header/banner widgets may not repaint automatically.
        wizard = self.wizard()
        if wizard:
            wizard.style().unpolish(wizard)
            wizard.style().polish(wizard)
            wizard.update()

    def get_selected_theme(self) -> str:
        """Return selected theme ID"""
        current = self.theme_list.currentItem()
        if current:
            meta = current.data(Qt.ItemDataRole.UserRole)
            if meta:
                return meta.get("id", "system")
        return "system"

    def validatePage(self) -> bool:
        """Always valid - theme selection is optional"""
        return True

    def cleanupPage(self) -> None:
        """Restore original theme if user goes back"""
        self.theme_manager.apply_theme(self._original_theme)

    def nextId(self) -> int:
        return PAGE_STORES


# ── Page 3: Store Selection ──────────────────────────────────────────

class StoreSelectionPage(QWizardPage):
    """Select which store plugins to enable — at least one required."""

    def __init__(self, plugin_manager: PluginManager, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.plugin_manager = plugin_manager

        self.setTitle(_("Game Stores"))
        self.setSubTitle(_("Select the game stores you want to connect"))

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── Page icon ──
        icons_dir = _wizard_icons_dir()
        self._lbl_intro = None
        icon_path = icons_dir / "wizard_stores.svg"
        if icon_path.exists():
            icon_row = QHBoxLayout()
            icon_label = QLabel()
            icon_label.setPixmap(_load_themed_svg(icon_path, size=32))
            icon_label.setFixedSize(32, 32)
            icon_row.addWidget(icon_label)

            self._lbl_intro = QLabel(
                _("Pick the stores where you own games. You can add more later in Settings.")
            )
            self._lbl_intro.setWordWrap(True)
            icon_row.addWidget(self._lbl_intro, 1)
            layout.addLayout(icon_row)
            layout.addSpacing(4)

        # Store selection in a group box for visual grouping
        store_group = QGroupBox()
        store_layout = QVBoxLayout(store_group)
        store_layout.setSpacing(8)

        # Store checkboxes (auto-generated from store plugins)
        self.store_checkboxes: Dict[str, QCheckBox] = {}

        # Get all store plugins from plugin manager
        store_plugins = plugin_manager.get_plugins_by_type(PluginType.STORE)

        for plugin_name, plugin_instance in store_plugins.items():
            display_name = getattr(
                plugin_instance, 'display_name',
                PluginManager.get_store_display_name(plugin_name),
            )
            description = ""

            # Get description from plugin metadata
            discovered = plugin_manager.get_discovered_plugins()
            if plugin_name in discovered:
                description = discovered[plugin_name].description

            cb = QCheckBox(display_name)
            if description:
                cb.setToolTip(description)
            cb.setProperty("plugin_id", plugin_name)

            self.store_checkboxes[plugin_name] = cb
            store_layout.addWidget(cb)

        layout.addWidget(store_group)
        layout.addStretch()

        # Hint about metadata
        self._lbl_hint = QLabel(
            _("Game metadata (covers, descriptions, ratings) is fetched automatically.\n"
              "Additional options can be configured in Settings later.")
        )
        self._lbl_hint.setObjectName("wizardHint")
        self._lbl_hint.setWordWrap(True)
        layout.addWidget(self._lbl_hint)

    def retranslateUi(self) -> None:
        """Update all translatable text after language change."""
        self.setTitle(_("Game Stores"))
        self.setSubTitle(_("Select the game stores you want to connect"))
        if self._lbl_intro:
            self._lbl_intro.setText(
                _("Pick the stores where you own games. You can add more later in Settings.")
            )
        self._lbl_hint.setText(
            _("Game metadata (covers, descriptions, ratings) is fetched automatically.\n"
              "Additional options can be configured in Settings later.")
        )

    def get_enabled_stores(self) -> List[str]:
        """Return list of enabled store plugin IDs"""
        return [pid for pid, cb in self.store_checkboxes.items() if cb.isChecked()]

    def validatePage(self) -> bool:
        """At least one store must be selected"""
        if not any(cb.isChecked() for cb in self.store_checkboxes.values()):
            QMessageBox.warning(
                self,
                _("Selection Required"),
                _("Please select at least one game store to continue.\n\n"
                  "{app_name} needs at least one store to show your games.").format(
                    app_name=APP_NAME)
            )
            return False
        return True

    def nextId(self) -> int:
        return PAGE_CREDENTIALS


# ── Page 4: Credentials ─────────────────────────────────────────────

class CredentialsPage(QWizardPage):
    """Connect accounts for selected stores via per-store Login flows."""

    def __init__(
        self,
        plugin_manager: PluginManager,
        config: Config,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self.plugin_manager = plugin_manager
        self.config = config

        self.setTitle(_("Connect Your Accounts"))
        self.setSubTitle(
            _("Log in to each store to verify your account")
        )

        self._layout = QVBoxLayout(self)
        self._sections: Dict[str, QGroupBox] = {}
        # Per-store widgets: {plugin_id: {status_icon, status_label, login_btn, ...}}
        self._store_widgets: Dict[str, Dict[str, Any]] = {}
        self._plugin_cache: Dict[str, Any] = {}
        self._enabled_stores: List[str] = []

    # ── Page lifecycle ───────────────────────────────────────────────

    def initializePage(self) -> None:
        """Build UI based on stores selected in page 3."""
        self._clear_sections()

        # Page icon + intro
        icons_dir = _wizard_icons_dir()
        icon_path = icons_dir / "wizard_credentials.svg"
        if icon_path.exists():
            icon_row = QHBoxLayout()
            icon_label = QLabel()
            icon_label.setPixmap(_load_themed_svg(icon_path, size=32))
            icon_label.setFixedSize(32, 32)
            icon_row.addWidget(icon_label)

            self._lbl_intro = QLabel(
                _("Connect each store so {app_name} can access your game library.").format(
                    app_name=APP_NAME)
            )
            self._lbl_intro.setWordWrap(True)
            icon_row.addWidget(self._lbl_intro, 1)
            self._layout.addLayout(icon_row)

        # Get selected stores
        wizard = self.wizard()
        if hasattr(wizard, 'store_page'):
            self._enabled_stores = wizard.store_page.get_enabled_stores()
        else:
            self._enabled_stores = []

        for plugin_id in self._enabled_stores:
            self._add_store_section(plugin_id)

        self._layout.addStretch()

        # Apply consent early so cookie reading works during the wizard
        # (consent is normally only saved in accept(), but BrowserCookieManager
        # checks it before reading cookies)
        if hasattr(wizard, 'consent_page') and wizard.consent_page.is_consent_granted():
            self.config.set("privacy.local_data_access_consent", True)

        # Check auth state for all stores
        self._refresh_all_statuses()

    def _clear_sections(self) -> None:
        """Remove all dynamic widgets from previous initializePage calls."""
        for group in self._sections.values():
            self._layout.removeWidget(group)
            group.deleteLater()
        self._sections.clear()
        self._store_widgets.clear()

        # Remove icon row / stretch items
        for i in reversed(range(self._layout.count())):
            item = self._layout.itemAt(i)
            if item and item.widget():
                self._layout.removeWidget(item.widget())
                item.widget().deleteLater()
            elif item and item.layout():
                sub = item.layout()
                while sub.count():
                    child = sub.takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()
                self._layout.removeItem(item)

    # ── Per-store section ────────────────────────────────────────────

    def _add_store_section(self, plugin_id: str) -> None:
        """Add a Login section for one store."""
        display_name = PluginManager.get_store_display_name(plugin_id)
        group = QGroupBox(display_name)
        vbox = QVBoxLayout(group)

        # Status row: [icon] [status text] ............ [Login]
        status_row = QHBoxLayout()

        icon_label = QLabel()
        icon_label.setFixedSize(16, 16)
        status_row.addWidget(icon_label)

        status_label = QLabel(_("Checking..."))
        status_label.setWordWrap(True)
        status_row.addWidget(status_label, 1)

        login_btn = QPushButton(_("Login"))
        login_btn.clicked.connect(lambda _checked=False, pid=plugin_id: self._on_login(pid))
        status_row.addWidget(login_btn)

        vbox.addLayout(status_row)

        widgets: Dict[str, Any] = {
            "status_icon": icon_label,
            "status_label": status_label,
            "login_btn": login_btn,
            "group": group,
        }

        # Steam: inline credential fields (hidden until Login is clicked)
        if plugin_id == "steam":
            self._build_steam_fields(vbox, widgets)

        self._store_widgets[plugin_id] = widgets
        self._sections[plugin_id] = group
        self._layout.addWidget(group)

    def _build_steam_fields(self, parent_layout: QVBoxLayout, widgets: Dict[str, Any]) -> None:
        """Build inline SteamID + API key fields for Steam."""
        container = QWidget()
        container.setVisible(False)
        form = QVBoxLayout(container)
        form.setContentsMargins(0, 8, 0, 0)

        # SteamID row
        id_row = QHBoxLayout()
        id_label = QLabel(_("SteamID:"))
        id_label.setMinimumWidth(100)
        id_row.addWidget(id_label)
        steam_id_edit = QLineEdit()
        steam_id_edit.setPlaceholderText("76561198000000000")
        # Pre-fill from config/keyring
        existing_id = self.config.get("plugins.steam.steam_id")
        if not existing_id:
            existing_id = self.plugin_manager.credential_manager.get("steam", "steam_id")
        if existing_id:
            steam_id_edit.setText(str(existing_id))
        id_row.addWidget(steam_id_edit, 1)
        form.addLayout(id_row)

        # API key row
        key_row = QHBoxLayout()
        key_label = QLabel(_("API Key:"))
        key_label.setMinimumWidth(100)
        key_row.addWidget(key_label)
        api_key_edit = QLineEdit()
        api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        existing_key = self.plugin_manager.credential_manager.get("steam", "api_key")
        if existing_key:
            api_key_edit.setText(existing_key)
        key_row.addWidget(api_key_edit, 1)

        toggle_btn = QPushButton("\U0001f441")
        toggle_btn.setObjectName("wizardPasswordToggle")
        toggle_btn.setCheckable(True)
        toggle_btn.setFixedSize(28, 28)
        toggle_btn.setToolTip(_("Show/hide"))
        toggle_btn.toggled.connect(
            lambda checked, e=api_key_edit: e.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        key_row.addWidget(toggle_btn)

        link = QLabel(
            '<a href="https://steamcommunity.com/dev/apikey">'
            + _("Get your key") + '</a>'
        )
        from ...utils.browser import open_url as _open_url
        link.linkActivated.connect(_open_url)
        link.setObjectName("wizardHelpLink")
        key_row.addWidget(link)
        form.addLayout(key_row)

        parent_layout.addWidget(container)

        widgets["steam_container"] = container
        widgets["steam_id_edit"] = steam_id_edit
        widgets["api_key_edit"] = api_key_edit

    # ── Login button dispatch ────────────────────────────────────────

    def _on_login(self, plugin_id: str) -> None:
        """Route to the correct auth flow for each store."""
        if plugin_id == "epic":
            self._on_epic_login()
        elif plugin_id == "gog":
            self._on_gog_login()
        elif plugin_id == "steam":
            self._on_steam_login()
        else:
            # Generic fallback: just load and check is_authenticated
            store = self._get_plugin_instance(plugin_id)
            if store and store.is_authenticated():
                self._set_status(plugin_id, True,
                                 _("Connected"))
            else:
                self._set_status(plugin_id, False,
                                 _("Not connected"))

    # ── Epic auth flow ───────────────────────────────────────────────

    def _on_epic_login(self) -> None:
        """Epic OAuth authorization code flow."""
        from ...plugins.epic.config_dialog import (
            _DEFAULT_CLIENT_ID,
            _EPIC_LOGIN_URL_TEMPLATE,
        )

        instructions = (
            "<b>" + _("Epic Games Authentication") + "</b><br><br>"
            + _("To connect your Epic Games account:") + "<br><br>"
            "<b>1.</b> " + _("Click OK to open the Epic Games login page") + "<br><br>"
            "<b>2.</b> " + _("Log in with your Epic Games account") + "<br><br>"
            "<b>3.</b> " + _("After login, you'll see a JSON response. Find the "
                             "<code>authorizationCode</code> value") + "<br><br>"
            "<b>4.</b> " + _("Copy that code and paste it in the next dialog") + "<br><br>"
            "<b>" + _("Example JSON:") + "</b><br>"
            "<code>{\"authorizationCode\":\"abc123...\"}</code>"
        )

        reply = QMessageBox.question(
            self,
            _("Epic Games Login"),
            instructions,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Ok:
            return

        # Build login URL
        client_id = _DEFAULT_CLIENT_ID
        store = self._get_plugin_instance("epic")
        if store:
            stored_id = store.get_credential("epic_client_id")
            if stored_id:
                client_id = stored_id

        login_url = _EPIC_LOGIN_URL_TEMPLATE.format(client_id=client_id)
        from ...utils.browser import open_url
        open_url(login_url)

        code, ok = QInputDialog.getText(
            self,
            _("Enter Authorization Code"),
            _("Paste the authorizationCode from the JSON response:\n\n"
              "(The code is a long string of letters and numbers)"),
            QLineEdit.EchoMode.Normal,
            ""
        )

        if not ok or not code.strip():
            return

        self._set_status("epic", None, _("Authenticating..."))
        QApplication.processEvents()

        if not store:
            self._set_status("epic", False, _("Plugin not loaded"))
            return

        success, message = store.authenticate_with_code(code.strip())
        if success:
            self._set_status("epic", True, message)
        else:
            self._set_status("epic", False,
                             _("Authentication failed: {message}").format(message=message))

    # ── GOG auth flow ────────────────────────────────────────────────

    def _on_gog_login(self) -> None:
        """GOG browser cookie login via BrowserLoginDialog."""
        store = self._get_plugin_instance("gog")
        if not store:
            self._set_status("gog", False, _("Plugin not loaded"))
            return

        login_config = store.get_login_config()
        if not login_config:
            self._set_status("gog", False, _("Login not supported"))
            return

        from .oauth_dialog import BrowserLoginDialog
        dialog = BrowserLoginDialog(login_config, self)
        result = dialog.exec()
        cookies = dialog.get_cookies()
        dialog.deleteLater()

        if cookies:
            store.handle_cookies(cookies)
            self._update_store_status("gog")
        elif result == 0:
            # Cancelled — don't change status if already authenticated
            if not store.is_authenticated():
                self._set_status("gog", False, _("Not connected"))

    # ── Steam auth flow ──────────────────────────────────────────────

    def _on_steam_login(self) -> None:
        """Show Steam credential fields, or save+verify if already visible."""
        widgets = self._store_widgets.get("steam", {})
        container = widgets.get("steam_container")
        if not container:
            return

        if not container.isVisible():
            # First click: show fields, change button text
            container.setVisible(True)
            btn = widgets.get("login_btn")
            if btn:
                btn.setText(_("Connect"))
            # Focus the first empty field
            steam_id_edit = widgets.get("steam_id_edit")
            api_key_edit = widgets.get("api_key_edit")
            if steam_id_edit and not steam_id_edit.text().strip():
                steam_id_edit.setFocus()
            elif api_key_edit:
                api_key_edit.setFocus()
        else:
            # Second click: save and verify
            self._on_steam_verify()

    def _on_steam_verify(self) -> None:
        """Save Steam credentials and verify authentication."""
        widgets = self._store_widgets.get("steam", {})
        steam_id_edit = widgets.get("steam_id_edit")
        api_key_edit = widgets.get("api_key_edit")

        if not steam_id_edit or not api_key_edit:
            return

        steam_id = steam_id_edit.text().strip()
        api_key = api_key_edit.text().strip()

        if not steam_id or not api_key:
            self._set_status("steam", False,
                             _("Both SteamID and API Key are required"))
            return

        # Store credentials
        self.config.set("plugins.steam.steam_id", steam_id)
        self.plugin_manager.credential_manager.store("steam", "api_key", api_key)
        self.config.save()

        self._set_status("steam", None, _("Verifying..."))
        QApplication.processEvents()

        # Load plugin and refresh its in-memory settings
        store = self._get_plugin_instance("steam")
        if not store:
            self._set_status("steam", False, _("Plugin not loaded"))
            return
        store.set_settings(self.config.get_plugin_settings("steam"))

        try:
            result = asyncio.run(store.authenticate())
            if result:
                self._update_store_status("steam")
            else:
                self._set_status("steam", False,
                                 _("Authentication failed — check your credentials"))
        except Exception as exc:
            self._set_status("steam", False, str(exc))

    # ── Plugin loading ───────────────────────────────────────────────

    def _get_plugin_instance(self, plugin_id: str):
        """Get or load a plugin instance (cached across page visits)."""
        if plugin_id in self._plugin_cache:
            return self._plugin_cache[plugin_id]

        loaded = self.plugin_manager._loaded.get(plugin_id)
        if not loaded or not loaded.instance:
            try:
                self.plugin_manager.load_plugin(plugin_id)
                loaded = self.plugin_manager._loaded.get(plugin_id)
            except Exception as exc:
                logger.warning("Failed to load plugin %s: %s", plugin_id, exc)
                return None

        if loaded and loaded.instance:
            self._plugin_cache[plugin_id] = loaded.instance
            return loaded.instance
        return None

    # ── Status management ────────────────────────────────────────────

    def _refresh_all_statuses(self) -> None:
        """Check auth state for all enabled stores."""
        for plugin_id in self._enabled_stores:
            self._update_store_status(plugin_id)

    def _update_store_status(self, plugin_id: str) -> None:
        """Query a plugin's auth status and update the UI."""
        store = self._get_plugin_instance(plugin_id)
        if not store:
            self._set_status(plugin_id, False, _("Plugin not loaded"))
            return

        if hasattr(store, 'get_auth_status'):
            is_auth, status_msg = store.get_auth_status()
        else:
            is_auth = store.is_authenticated()
            status_msg = _("Connected") if is_auth else _("Not connected")

        self._set_status(plugin_id, is_auth, status_msg)

        # Update login button text
        widgets = self._store_widgets.get(plugin_id, {})
        btn = widgets.get("login_btn")
        if btn:
            btn.setText(_("Re-login") if is_auth else _("Login"))

    def _set_status(self, plugin_id: str, ok: Optional[bool], text: str) -> None:
        """Set status icon + label for a store. ok=None for neutral/pending."""
        widgets = self._store_widgets.get(plugin_id, {})
        icon_label = widgets.get("status_icon")
        status_label = widgets.get("status_label")

        if status_label:
            status_label.setText(text)
            from luducat.utils.style_helpers import set_status_property
            if ok is True:
                set_status_property(status_label, "success")
            elif ok is False:
                set_status_property(status_label, "error")
            else:
                set_status_property(status_label, "")

        if icon_label:
            icons_dir = _wizard_icons_dir()
            if ok is True:
                pm = _load_themed_svg(icons_dir / "status_ok.svg", size=16)
            elif ok is False:
                pm = _load_themed_svg(icons_dir / "status_fail.svg", size=16)
            else:
                pm = QPixmap()
            icon_label.setPixmap(pm)

        self.completeChanged.emit()

    # ── Next> gating ─────────────────────────────────────────────────

    def isComplete(self) -> bool:
        """All enabled stores must be authenticated before Next> is enabled."""
        for plugin_id in self._enabled_stores:
            store = self._plugin_cache.get(plugin_id)
            if not store:
                return False
            if not store.is_authenticated():
                return False
        return True

    def validatePage(self) -> bool:
        """No extra validation — isComplete already gates progression."""
        return True

    def retranslateUi(self) -> None:
        """Update translatable text after language change."""
        self.setTitle(_("Connect Your Accounts"))
        self.setSubTitle(
            _("Log in to each store to verify your account")
        )
        if hasattr(self, '_lbl_intro'):
            self._lbl_intro.setText(
                _("Connect each store so {app_name} can access your game library.").format(
                    app_name=APP_NAME)
            )

    def nextId(self) -> int:
        wizard = self.wizard()
        if wizard and getattr(wizard, '_quick_setup', False):
            return PAGE_UPDATE
        return PAGE_BACKUP


# ── Page 5: Backup Settings ─────────────────────────────────────────

class BackupConfigPage(QWizardPage):
    """Backup configuration — full wizard only.

    Quick setup enables backups with default location automatically.
    """

    def __init__(
        self,
        config: Config,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.config = config

        self.setTitle(_("Backups"))
        self.setSubTitle(_("Protect your library data"))

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── Page icon ──
        icons_dir = _wizard_icons_dir()
        self._lbl_intro = None
        icon_path = icons_dir / "wizard_backup.svg"
        if icon_path.exists():
            icon_row = QHBoxLayout()
            icon_label = QLabel()
            icon_label.setPixmap(_load_themed_svg(icon_path, size=32))
            icon_label.setFixedSize(32, 32)
            icon_row.addWidget(icon_label)

            self._lbl_intro = QLabel(
                _("{app_name} can automatically back up your library database "
                  "and settings.").format(app_name=APP_NAME)
            )
            self._lbl_intro.setWordWrap(True)
            icon_row.addWidget(self._lbl_intro, 1)
            layout.addLayout(icon_row)
            layout.addSpacing(4)

        self.enable_checkbox = QCheckBox(_("Enable automatic backups"))
        self.enable_checkbox.setChecked(True)  # Default: enabled
        self.enable_checkbox.stateChanged.connect(self._on_enable_changed)
        layout.addWidget(self.enable_checkbox)

        # Path selection row
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        _default_display = str(get_default_backup_dir()).replace(
            str(Path.home()), "~", 1)
        self.path_edit.setPlaceholderText(
            _("Default: {path}").format(path=_default_display)
        )
        path_row.addWidget(self.path_edit, 1)

        self.browse_btn = QPushButton(_("Browse..."))
        self.browse_btn.clicked.connect(self._browse_path)
        path_row.addWidget(self.browse_btn)
        layout.addLayout(path_row)

        self._lbl_hint = QLabel(
            _("You can configure detailed scheduling "
              "and retention in Settings later.")
        )
        self._lbl_hint.setObjectName("hintLabel")
        self._lbl_hint.setWordWrap(True)
        layout.addWidget(self._lbl_hint)

        layout.addStretch()

    def retranslateUi(self) -> None:
        """Update all translatable text after language change."""
        self.setTitle(_("Backups"))
        self.setSubTitle(_("Protect your library data"))
        if self._lbl_intro:
            self._lbl_intro.setText(
                _("{app_name} can automatically back up your library database "
                  "and settings.").format(app_name=APP_NAME)
            )
        self.enable_checkbox.setText(_("Enable automatic backups"))
        _default_display = str(get_default_backup_dir()).replace(
            str(Path.home()), "~", 1)
        self.path_edit.setPlaceholderText(
            _("Default: {path}").format(path=_default_display)
        )
        self.browse_btn.setText(_("Browse..."))
        self._lbl_hint.setText(
            _("You can configure detailed scheduling and retention in Settings later.")
        )

    def _on_enable_changed(self, state: int) -> None:
        """Enable/disable path selection when checkbox is toggled"""
        enabled = state == Qt.CheckState.Checked.value
        self.path_edit.setEnabled(enabled)
        self.browse_btn.setEnabled(enabled)

    def _browse_path(self) -> None:
        """Open directory browser for backup location"""
        current = self.path_edit.text() or str(Path.home())
        path = QFileDialog.getExistingDirectory(
            self,
            _("Select Backup Location"),
            current,
            QFileDialog.Option.ShowDirsOnly,
        )
        if path:
            self.path_edit.setText(path)

    def is_backup_enabled(self) -> bool:
        """Return whether automatic backups are enabled"""
        return self.enable_checkbox.isChecked()

    def get_backup_path(self) -> str:
        """Return the configured backup path (empty string if not set)"""
        return self.path_edit.text().strip()

    def nextId(self) -> int:
        return PAGE_UPDATE


# ── Page 6: Update Check ────────────────────────────────────────────

class UpdateOptInPage(QWizardPage):
    """Update notification opt-in — final page on both paths."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self.setTitle(_("Update Notifications"))
        self.setSubTitle(_("Stay informed about new versions"))
        self.setFinalPage(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        # Icon + explanation
        intro_layout = QHBoxLayout()
        intro_layout.setSpacing(12)

        icons_dir = _wizard_icons_dir()
        icon_label = QLabel()
        icon_path = icons_dir / "wizard_update.svg"
        if icon_path.exists():
            icon_label.setPixmap(_load_themed_svg(icon_path, size=48))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        icon_label.setFixedSize(48, 48)
        intro_layout.addWidget(icon_label)

        self._lbl_intro = QLabel(
            _("{app_name} can check for new versions on startup and after "
              "each sync. This is completely optional.").format(app_name=APP_NAME)
        )
        self._lbl_intro.setWordWrap(True)
        intro_layout.addWidget(self._lbl_intro, 1)

        layout.addLayout(intro_layout)

        # How it works
        self._lbl_details = QLabel()
        self._lbl_details.setWordWrap(True)
        self._lbl_details.setTextFormat(Qt.TextFormat.RichText)
        self._set_details_text()
        layout.addWidget(self._lbl_details)

        layout.addSpacing(8)

        # Opt-in checkbox
        self.chk_check_updates = QCheckBox(_("Check for updates on startup and after sync"))
        self.chk_check_updates.setChecked(False)
        layout.addWidget(self.chk_check_updates)

        layout.addStretch()

    def _set_details_text(self) -> None:
        """Set the 'How it works' details label text."""
        self._lbl_details.setText(
            "<b>" + _("How it works:") + "</b><br>"
            + _("\u2022 Connects to a Cloudflare-hosted server to check version "
                "numbers") + "<br>"
            + _("\u2022 No personal data, library info, or telemetry is ever sent") + "<br>"
            + _("\u2022 If a new version is found, you'll see a notification "
                "with a download link") + "<br>"
            + _("\u2022 Nothing is downloaded or installed automatically")
        )

    def retranslateUi(self) -> None:
        """Update all translatable text after language change."""
        self.setTitle(_("Update Notifications"))
        self.setSubTitle(_("Stay informed about new versions"))
        self._lbl_intro.setText(
            _("{app_name} can check for new versions on startup and after "
              "each sync. This is completely optional.").format(app_name=APP_NAME)
        )
        self._set_details_text()
        self.chk_check_updates.setText(_("Check for updates on startup and after sync"))

    def is_update_check_enabled(self) -> bool:
        """Return whether the user opted in to update checking"""
        return self.chk_check_updates.isChecked()

    def nextId(self) -> int:
        return -1


# ── SetupWizard ──────────────────────────────────────────────────────

class SetupWizard(QWizard):
    """First-run setup wizard with Quick Setup and Full Wizard paths.

    Quick Setup:  Welcome → Consent → Stores → Credentials → Update
    Full Wizard:  Welcome → Consent → Theme → Stores → Credentials → Backup → Update
    """

    def __init__(
        self,
        config: Config,
        plugin_manager: PluginManager,
        theme_manager: ThemeManager,
        parent: Optional[QWidget] = None,
        is_first_run: bool = True,
        is_rerun: bool = False,
    ):
        super().__init__(parent)
        self.config = config
        self.plugin_manager = plugin_manager
        self.theme_manager = theme_manager
        self.is_first_run = is_first_run
        self.is_rerun = is_rerun
        self._quick_setup = False

        # Remember the language active before the wizard opens so we can
        # revert if the user cancels after testing a different language.
        from ...core.i18n import get_current_language
        self._original_language = get_current_language()

        self.setWindowTitle(_("{app_name} Setup").format(app_name=APP_NAME))
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setMinimumSize(600, 500)

        # Set logo pixmap (appears in header on all pages)
        icon_path = Path(__file__).parent.parent.parent / "assets" / "appicons" / "app_icon.svg"
        if icon_path.exists():
            pixmap = QPixmap(str(icon_path))
            if not pixmap.isNull():
                pixmap = pixmap.scaled(
                    64, 64,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
                self.setPixmap(QWizard.WizardPixmap.LogoPixmap, pixmap)

        # Create pages
        self.welcome_page = WelcomePage(config)
        self.consent_page = DataAccessConsentPage(config)
        self.theme_page = ThemeSelectionPage(theme_manager)
        self.store_page = StoreSelectionPage(plugin_manager)
        self.credentials_page = CredentialsPage(plugin_manager, config)
        self.backup_page = BackupConfigPage(config)
        self.update_page = UpdateOptInPage()

        # Register pages with explicit IDs for routing
        self.setPage(PAGE_WELCOME, self.welcome_page)
        self.setPage(PAGE_CONSENT, self.consent_page)
        self.setPage(PAGE_THEME, self.theme_page)
        self.setPage(PAGE_STORES, self.store_page)
        self.setPage(PAGE_CREDENTIALS, self.credentials_page)
        self.setPage(PAGE_BACKUP, self.backup_page)
        self.setPage(PAGE_UPDATE, self.update_page)
        # Archive page not registered — feature not yet implemented

        # Track quick setup choice on page transitions
        self.currentIdChanged.connect(self._on_page_changed)

        # Pre-fill with existing settings if re-running wizard
        if is_rerun:
            self._prefill_existing_settings()

    def _on_page_changed(self, page_id: int) -> None:
        """Update quick setup flag when leaving the welcome page."""
        if page_id == PAGE_CONSENT:
            # Capture quick setup choice from welcome page
            self._quick_setup = self.welcome_page.is_quick_setup()

    def _rebuild_pages_for_language(self) -> None:
        """Rebuild translatable text on all wizard pages after language change.

        Called from WelcomePage when the user picks a different language.
        Updates all body content, labels, checkboxes, tooltips, and combo items.
        """
        # Reinstall Qt translator so wizard buttons (Next/Back/Cancel) update
        from ...core.i18n import install_qt_translator
        app = QApplication.instance()
        if app:
            install_qt_translator(app)

        # Update wizard window title
        self.setWindowTitle(_("{app_name} Setup").format(app_name=APP_NAME))

        # Retranslate all pages
        self.welcome_page.retranslateUi()
        self.consent_page.retranslateUi()
        self.theme_page.retranslateUi()
        self.store_page.retranslateUi()
        self.credentials_page.retranslateUi()
        self.backup_page.retranslateUi()
        self.update_page.retranslateUi()

    def _prefill_existing_settings(self) -> None:
        """Pre-fill checkboxes based on currently enabled plugins"""
        plugins_config = self.config.get("plugins", {})

        for plugin_id, cb in self.store_page.store_checkboxes.items():
            plugin_settings = plugins_config.get(plugin_id, {})
            enabled = plugin_settings.get("enabled", True)
            cb.setChecked(enabled)

    def accept(self) -> None:
        """Save all wizard settings when finished"""
        # Credentials saved inline during login flows (Steam verify, Epic code, GOG cookies)

        # Save language
        self.config.set("app.language", self.welcome_page.get_selected_language())

        # Save consent
        self.config.set(
            "privacy.local_data_access_consent",
            self.consent_page.is_consent_granted(),
        )

        # Refresh consent on all plugins immediately
        self.plugin_manager.refresh_all_consent()

        # Save theme
        if self._quick_setup:
            # Quick setup: system theme
            self.config.set("appearance.theme", "system")
        else:
            selected_theme = self.theme_page.get_selected_theme()
            self.config.set("appearance.theme", selected_theme)

        # Save enabled stores
        for plugin_id, cb in self.store_page.store_checkboxes.items():
            self.config.set(f"plugins.{plugin_id}.enabled", cb.isChecked())

        # Save content filter preference (on consent page)
        self.config.set(
            "content_filter.enabled",
            self.consent_page.is_content_filter_enabled(),
        )

        # Save update check opt-in
        self.config.set(
            "app.check_for_updates",
            self.update_page.is_update_check_enabled(),
        )

        # Save backup settings
        if self._quick_setup:
            # Quick setup: backups enabled with default location
            self.config.set("backup.auto_backup_on_exit", True)
            self.config.set("backup.location", str(get_default_backup_dir()))
        else:
            if self.backup_page.is_backup_enabled():
                self.config.set("backup.auto_backup_on_exit", True)
                backup_path = self.backup_page.get_backup_path()
                if backup_path:
                    self.config.set("backup.location", backup_path)

        # Mark first run as complete
        if self.is_first_run:
            self.config.mark_first_run_complete()

        self.config.save()
        logger.info("Setup wizard completed (quick_setup=%s)", self._quick_setup)
        super().accept()

    def reject(self) -> None:
        """Revert live language preview when the wizard is cancelled."""
        from ...core.i18n import get_current_language, init_i18n

        if get_current_language() != self._original_language:
            init_i18n(self._original_language)
            logger.info("Wizard cancelled — reverted language to '%s'",
                        self._original_language)
        super().reject()

    def get_enabled_stores(self) -> List[str]:
        """Get list of enabled store plugin IDs"""
        return self.store_page.get_enabled_stores()

    def get_selected_theme(self) -> str:
        """Get selected theme ID"""
        if self._quick_setup:
            return "system"
        return self.theme_page.get_selected_theme()
