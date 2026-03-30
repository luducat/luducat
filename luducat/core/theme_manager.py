# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# theme_manager.py

"""Theme manager for luducat

Handles:
- System theme detection (respects OS dark/light mode)
- Custom QSS theme loading from ~/.config/luducat/themes/
- UI zoom/scaling via QT_SCALE_FACTOR (requires restart)
"""

import hashlib
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

from luducat.core.json_compat import json

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette, QFont, QColor
from PySide6.QtCore import QObject, Signal

from .config import get_config_dir

logger = logging.getLogger(__name__)

# Theme constants
THEME_SYSTEM = "system"  # Use system palette (respects dark/light mode)

# Default zoom
DEFAULT_ZOOM = 100

# Minimal system default stylesheet - embedded to ensure it's always available
# IMPORTANT: Do NOT add font-size declarations - let system font settings apply
# Only exceptions: headers (16pt), emoji icons (18pt), icon fallback (36pt)
SYSTEM_DEFAULT_QSS = """
/* ========================================
   GLOBAL DEFAULTS - use system font size, ensure palette colors
   ======================================== */
QLabel, QCheckBox, QRadioButton {
    color: palette(text);
}

QLabel {
    font-weight: 400;
}

QPushButton {
    min-height: 22px;
}

QPushButton:disabled {
    color: palette(mid);
    background-color: palette(base);
}

QComboBox {
    margin-bottom: 2px;
}

QPushButton#filterChip {
    min-height: 20px;
}

#filterBarWidget QPushButton#tagChip {
    background: transparent;
    border: none;
    border-bottom: 1px solid transparent;
    border-radius: 0px;
    padding: 3px 8px;
    min-height: 18px;
}

#filterBarWidget QPushButton#tagChip:hover {
    background: transparent;
    border: none;
    border-bottom: 1px solid transparent;
}

/* ========================================
   MENUS - Hover highlight for QMenu items
   ======================================== */
QMenu::item {
    padding: 4px 20px;
}

QMenu::item:selected {
    background-color: palette(highlight);
    color: palette(highlighted-text);
}

QMenu::separator {
    height: 1px;
    background: palette(mid);
    margin: 4px 8px;
}

/* Checkboxes embedded in menus (QWidgetAction) */
QMenu QCheckBox {
    background: transparent;
    padding: 4px 20px;
    spacing: 6px;
}

QMenu QCheckBox:hover {
    background-color: palette(midlight);
}

QMenu QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid palette(mid);
    border-radius: 2px;
    background-color: palette(base);
}

QMenu QCheckBox::indicator:checked {
    background-color: palette(highlight);
    border-color: palette(highlight);
}

/* ========================================
   PROGRESS BAR CHUNK - fallback for loading overlay
   ======================================== */
QProgressBar::chunk {
    background-color: palette(highlight);
}

/* ========================================
   TOOLBAR & TOP ROW BUTTONS - Balanced size
   ======================================== */
#viewModeGroup QPushButton,
#sortButton, #refreshButton, #syncButton,
#settingsButton, #filterDropdownButton,
#toolsButton, #randomButton {
    padding: 6px 12px;
    min-height: 22px;
}

#toolbarSeparator {
    color: palette(mid);
}

#aboutButton {
    min-width: 28px;
    max-width: 32px;
    padding: 4px 6px;
    font-weight: bold;
}

/* ========================================
   SYNC WIDGET - Status bar sync progress
   ======================================== */
#syncWidget {
    background: palette(window);
    border-top: 1px solid palette(mid);
}

#syncProgressBar {
    min-height: 22px;
    text-align: center;
    background: palette(button);
    border: 1px solid palette(dark);
    border-radius: 3px;
}
#syncProgressBar::chunk {
    background-color: palette(highlight);
    border-radius: 2px;
}

#syncPauseBtn, #syncSkipBtn, #syncCancelBtn {
    padding: 2px 8px;
    min-height: 20px;
}

#syncSummaryLabel {
    color: palette(text);
}

#refreshButton {
    border: none;
    background: transparent;
    padding: 2px;
    min-width: 24px;
    min-height: 24px;
}

#refreshButton:hover {
    background: palette(midlight);
    border-radius: 3px;
}

/* ========================================
   FILTER CHIPS - Medium touch targets
   ======================================== */
QPushButton#filterChip {
    padding: 4px 10px;
    min-height: 22px;
}

/* ========================================
   FAVORITE BUTTON - Brighter + transparent hover
   ======================================== */
QPushButton#favoriteButton {
    padding: 6px 14px;
    border: 2px solid #f1c40f;
    border-radius: 6px;
    background: transparent;
    color: #f1c40f;
    font-weight: bold;
}

QPushButton#favoriteButton:checked {
    background: #ffeb3b;           /* Even brighter yellow when active */
    color: #1a1a1a;
    border-color: #fadc15;
}

QPushButton#favoriteButton:hover:!checked {
    background: rgba(250, 220, 21, 0.2);    /* Very transparent yellow tint */
    color: #f1c40f;
    border-color: #f1c40f;
}

QPushButton#favoriteButton:hover:checked {
    background: rgba(255, 235, 59, 0.85);   /* Brighter yellow, slightly transparent */
    color: #1a1a1a;
    border-color: #fadc15;
}

QPushButton#favoriteButton:pressed {
    background: #e8b923;           /* Solid for press feedback */
    color: #1a1a1a;
    border-color: #d4a017;
}


/* ========================================
   SHARED INDICATOR ICON - emoji needs explicit size
   ======================================== */
QLabel#borrowedFromLabel {
    font-size: 18pt;      /* Emoji/icon needs explicit size */
    color: #ff9800;       /* Orange for shared */
    font-weight: 900;     /* Bold emoji */
    min-width: 32pt;
    min-height: 32pt;
    padding: 4pt;
    qproperty-alignment: AlignCenter;
    background: transparent;
    border: none;
}


/* ========================================
   ACTION BUTTONS - Medium size
   ======================================== */
QPushButton#moreOptionsButton,
QPushButton#tagsButton,
QToolButton#storeButton {
    padding: 6px 14px;
    border-radius: 4px;
    min-height: 24px;
    text-align: center;
}

/* Import button (QToolButton with menu, styled to match QPushButton siblings) */
QToolButton#importButton {
    min-height: 22px;
    padding: 4px 8px;
}
QToolButton#importButton::menu-button {
    width: 16px;
}

/* ========================================
   LABELS & TEXT ELEMENTS - Fixed contrast
   ======================================== */
QLabel#releaseDateLabel,
QLabel#metadataLabel {
    padding: 0 6px;
    color: palette(text);
}

QLabel#dialogDescription,
QLabel#fieldDescription {
    color: palette(text);  /* Use palette for dark/light mode */
}

/* ========================================
   PLUGIN STATUS INDICATORS - Semantic colors
   ======================================== */
QLabel#pluginStatusConfigured { color: #27ae60; }
QLabel#pluginStatusNotConfigured { color: #f39c12; }
QLabel#pluginStatusDisabled { color: #e74c3c; }
QLabel#pluginStatusInDevelopment { color: #7f8c8d; }
QLabel#pluginStatusNotAvailable { color: #95a5a6; }
QLabel#pluginTypeBadge {
    color: palette(text);
    margin-right: 4px;
}
QFrame#gridSeparator {
    border: none;
    border-top: 1px solid palette(mid);
    max-height: 1px;
}
QDialog QLineEdit#launcherPathEdit,
QDialog QLineEdit#launcherPathEditAuto {
    background: transparent;
    border: 1px solid palette(mid);
    border-radius: 3px;
    padding: 0px 4px;
}
QDialog QLineEdit#launcherPathEditAuto {
    font-style: italic;
    color: palette(dark);
}

/* ========================================
   BACKUP PROGRESS DIALOG
   ======================================== */
QLabel#backupProgressStatus { font-weight: bold; }
QLabel#backupProgressSuccess { font-weight: bold; color: #27ae60; }
QLabel#backupProgressError { font-weight: bold; color: #e74c3c; }

/* ========================================
   DIALOG SLIDERS / CHECKBOXES - transparent background
   ======================================== */
QDialog QSlider {
    background: transparent;
}
QDialog QCheckBox {
    background: transparent;
}
QDialog QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid palette(mid);
    border-radius: 3px;
    background-color: palette(base);
}
QDialog QCheckBox::indicator:checked {
    background-color: palette(highlight);
    border-color: palette(highlight);
}

QDialog QRadioButton::indicator:unchecked {
    width: 12px;
    height: 12px;
    border: 2px solid palette(mid);
    border-radius: 8px;
    background-color: palette(base);
}

QDialog QRadioButton::indicator:checked {
    width: 12px;
    height: 12px;
    border: 2px solid palette(highlight);
    border-radius: 8px;
    background-color: palette(highlight);
}

/* ========================================
   SETTINGS HINT LABELS
   ======================================== */
QLabel#hintLabel {
    color: palette(text);  /* Consistent with rest of dialogs */
}

/* ========================================
   GROUP BOX TITLES - Bold, standard size
   ======================================== */
QGroupBox {
    margin-top: 10px;
    padding-top: 14px;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 8px;
    font-weight: bold;
    color: palette(text);
    padding: 2px 6px;
    background: palette(window);
}

QDialog QGroupBox {
    margin-top: 16px;
    padding-top: 16px;
}

QDialog QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    font-weight: bold;
    color: palette(text);
    padding: 2px 8px;
    background: palette(window);
}

/* ========================================
   DETAIL VIEW HEADER - Title bar with tabs
   ======================================== */
QFrame#listViewHeader {
    background-color: palette(base);
    border-bottom: 1px solid palette(dark);
}

/* ========================================
   HEADER TITLES - Large for detail view (keep 16pt for headers)
   ======================================== */
QLabel#gameTitle, QLabel#screenshots, QLabel#description {
    font-size: 16pt;
    font-weight: bold;
    color: palette(text);
    padding: 4px 0px;
    background: transparent;
}

QLabel#gameTitle {
    font-size: 16pt;
    font-weight: bold;
    color: palette(text);
    padding: 0px 0px;
    background: transparent;
}

/* ========================================
   ABOUT DIALOG - Headers (keep 16pt for title)
   ======================================== */
#aboutTitle {
    font-size: 16pt;
    font-weight: bold;
}
#aboutSubtitle {
    color: palette(text);
}
#aboutLink { }
#aboutIconFallback { font-size: 36pt; }  /* Large icon fallback */

/* ========================================
   ABOUT DIALOG - Content Boxes
   ======================================== */
#aboutDescBox, #aboutPluginsBox, #aboutLicenseBox {
    background-color: palette(base);
    border: 1px solid palette(dark);
    border-radius: 4px;
    padding: 8px;
}

#aboutBoxHeader { font-weight: bold; }
#aboutDescScroll, #aboutPluginsScroll {
    border: none;
    background: transparent;
}
#aboutLicenseText {
    font-size: 10pt;
    color: palette(text);
}

/* ========================================
   ABOUT DIALOG - Section Headers
   ======================================== */
#aboutSectionHeader {
    font-weight: bold;
    color: palette(text);
    background: palette(midlight);
    padding: 6px 8px;
    border-radius: 3px;
    margin-bottom: 6px;
}

/* ========================================
   CREDITS ITEMS
   ======================================== */
#creditItem {
    background-color: palette(base);
    border: 1px solid palette(dark);
    border-radius: 4px;
    margin-bottom: 6px;
}
#creditItemName {
    font-weight: bold;
    background: transparent;
    border: none;
    padding: 6px 8px;
}
#creditItemDesc {
    background: transparent;
    border: none;
    padding: 0 8px 6px 8px;
}
#creditItemLicense {
    color: palette(text);
    background: transparent;
    border: none;
    padding: 0 8px 4px 8px;
}

/* ========================================
   ABOUT DIALOG - Content Text & Plugin Items
   ======================================== */
#aboutDescText, #aboutBodyText {
    color: palette(text);
}
#aboutPluginItem {
    color: palette(text);
}
QTextBrowser#aboutNewsBrowser {
    background-color: palette(base);
    color: palette(text);
    border: none;
    padding: 8px;
}

/* ========================================
   UPDATE DIALOG / UPDATE LINKS
   ======================================== */
#aboutUpdateLink, #updateLink {
    color: palette(link);
}
QTextBrowser#updateChangelog {
    background-color: palette(base);
    color: palette(text);
    border: 1px solid palette(dark);
    border-radius: 4px;
    padding: 8px;
}

/* ========================================
   HERO BANNER - Background image with action buttons and carousel
   ======================================== */
#heroBanner {
    background-color: palette(window);
}
#heroBannerActions {
    background: transparent;
}
/* Action buttons on hero banner need semi-transparent bg for readability */
#heroBanner QPushButton, #heroBanner QToolButton {
    background-color: rgba(0, 0, 0, 0.55);
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.2);
    border-radius: 4px;
    padding: 4px 12px;
}
#heroBanner QPushButton:hover, #heroBanner QToolButton:hover {
    background-color: rgba(0, 0, 0, 0.7);
    border: 1px solid rgba(255, 255, 255, 0.35);
}
#heroBanner QPushButton:checked {
    background-color: rgba(60, 120, 200, 0.7);
    border: 1px solid rgba(100, 160, 240, 0.5);
}
#screenshotThumb {
    background-color: transparent;
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 4px;
}
#screenshotThumb:hover {
    border: 1px solid rgba(255, 255, 255, 0.5);
}

/* ========================================
   PRIMARY LAUNCH BUTTON - Styled like store button
   Use #heroBanner prefix for higher specificity
   ======================================== */
#heroBanner QPushButton#launchButtonPrimary,
#heroBanner QToolButton#launchButtonPrimary {
    background-color: palette(button);
    border: 1px solid palette(highlight);
    padding: 8px 16px;
    border-radius: 4px;
    color: palette(button-text);
    font-weight: bold;
}

#heroBanner QPushButton#launchButtonPrimary:hover,
#heroBanner QToolButton#launchButtonPrimary:hover {
    background-color: palette(highlight);
    color: palette(highlighted-text);
}

#heroBanner QPushButton#launchButtonPrimary:pressed,
#heroBanner QToolButton#launchButtonPrimary:pressed {
    background-color: palette(dark);
    color: palette(button-text);
}

#heroBanner QToolButton#launchButtonPrimary::menu-button {
    border-left: 1px solid palette(highlight);
    width: 20px;
}

QPushButton#launchButtonSecondary {
    background-color: palette(button);
    border: 1px solid palette(highlight);
    padding: 6px 14px;
}

/* Install button - muted style for uninstalled games */
#heroBanner QPushButton#launchButtonInstall,
#heroBanner QToolButton#launchButtonInstall {
    background-color: palette(button);
    border: 1px solid palette(mid);
    border-radius: 4px;
    padding: 8px 16px;
    color: palette(text);
    font-weight: normal;
}

#heroBanner QPushButton#launchButtonInstall:hover,
#heroBanner QToolButton#launchButtonInstall:hover {
    background-color: palette(light);
    color: palette(text);
}

#heroBanner QToolButton#launchButtonInstall::menu-button {
    border-left: 1px solid palette(mid);
    width: 20px;
}

/* Warning label */
QLabel#warningLabel {
    color: palette(highlight);
    padding: 4px 0;
}

/* ========================================
   ABOUT SPLITTER - Detail view content/metadata split
   ======================================== */
#aboutSplitter::handle {
    background: palette(dark);
    width: 1px;
}
#aboutSplitter::handle:hover {
    background: palette(highlight);
    width: 3px;
}

/* ========================================
   METADATA PANEL - Right-side detail panel
   ======================================== */
#metadataPanel, #metadataPanelContent {
    background-color: palette(base);
}
#metadataPanel {
    border: none;
}
QLabel#metadataKey {
    color: palette(text);
    font-weight: bold;
    padding: 1px 0;
}
QLabel#metadataValue {
    color: palette(text);
    padding: 1px 0;
}
QFrame#metadataSeparator {
    color: palette(mid);
}

/* ========================================
   FILTER CRUMBS - Removable filter indicator chips
   ======================================== */
QFrame#filterCrumb {
    background: palette(midlight);
    color: palette(text);
    border: 1px solid palette(mid);
    border-radius: 10px;
    padding: 2px 8px;
    min-height: 16px;
}
QFrame#filterCrumb:hover {
    background: palette(dark);
    color: palette(bright-text);
}
QLabel#filterCrumbLabel {
    background: transparent;
    color: inherit;
}

/* ========================================
   TAG CHIPS - Better contrast
   ======================================== */
QFrame#tagChip {
    background-color: palette(button);
    border: 1px solid palette(dark);
    border-radius: 10px;
    padding: 3px 8px;
}
QFrame#tagChip:hover {
    background-color: palette(midlight);
}
QLabel#tagChipLabel {
    color: palette(text);
    background: transparent;
}

/* Section headers (e.g., "Personal Notes") */
QLabel#sectionHeader { font-weight: bold; }

/* ========================================
   GAME MODE BADGES (detail panel header)
   ======================================== */
#gameModeBadge {
    background-color: palette(mid);
    color: palette(window-text);
    border-radius: 2px;
    padding: 1px 4px;
    font-weight: bold;
}

/* Family shared badge (detail panel header) */
QLabel#familySharedBadge {
    background-color: #8b6914;
    color: #ffffff;
    border-radius: 2px;
    padding: 1px 4px;
    font-weight: bold;
}

/* Installed badge (detail panel header) */
QLabel#installedBadge {
    background-color: #2e7d32;
    color: #ffffff;
    border-radius: 2px;
    padding: 1px 4px;
    font-weight: bold;
}

/* ========================================
   PROTONDB / STEAM DECK BADGES (detail panel header)
   Property-driven colors — set via setProperty("tier", value)
   ======================================== */
QLabel#protondbBadge, QLabel#steamDeckBadge {
    border-radius: 2px;
    padding: 1px 4px;
    font-weight: bold;
}

/* ProtonDB tier colors */
QLabel#protondbBadge[tier="platinum"] { background-color: #b4c7dc; color: #1a1a1a; }
QLabel#protondbBadge[tier="gold"]     { background-color: #cfb53b; color: #1a1a1a; }
QLabel#protondbBadge[tier="silver"]   { background-color: #a6a6a6; color: #1a1a1a; }
QLabel#protondbBadge[tier="bronze"]   { background-color: #cd7f32; color: #ffffff; }
QLabel#protondbBadge[tier="borked"]   { background-color: #ff0000; color: #ffffff; }

/* Steam Deck compat colors */
QLabel#steamDeckBadge[tier="verified"]    { background-color: #59bf40; color: #ffffff; }
QLabel#steamDeckBadge[tier="playable"]    { background-color: #ffc82c; color: #1a1a1a; }
QLabel#steamDeckBadge[tier="unsupported"] { background-color: #8b0000; color: #ffffff; }

/* ========================================
   IMAGE VIEWER DIALOG - Control bar
   ======================================== */
#imageViewerDialog { background-color: palette(window); }
#imageViewerControlBar {
    background-color: palette(base);
    border-top: 1px solid palette(dark);
}

#imageViewerControlBar QPushButton {
    background-color: palette(button);
    color: palette(button-text);
    border: 1px solid palette(mid);
    padding: 6px 12px;
    border-radius: 3px;
    min-width: 32px;
    min-height: 24px;
}

#imageViewerControlBar QPushButton:hover {
    background-color: palette(midlight);
}
#imageViewerControlBar QPushButton:pressed {
    background-color: palette(mid);
    color: palette(button-text);
}
#imageViewerControlBar QPushButton:disabled {
    color: palette(mid);
    background-color: palette(base);
}
#imageViewerControlBar QPushButton:checked {
    background-color: palette(highlight);
    color: palette(highlighted-text);
}

#imageViewerZoomLabel,
#imageViewerCounterLabel {
    color: palette(text);
}

/* ========================================
   STATUS BAR - Grid view footer
   ======================================== */
StatusBar {
    background: palette(window);
    border-top: 1px solid palette(mid);
}

#gameCount {
    color: palette(text);
}

#statusSeparator {
    color: palette(dark);
}

#networkIndicator {
    font-weight: bold;
    padding: 0 4px;
}

#densitySlider {
    height: 20px;
}

#densitySlider::groove:horizontal {
    border: 1px solid palette(mid);
    height: 4px;
    background: palette(base);
    border-radius: 2px;
}

#densitySlider::handle:horizontal {
    background: palette(highlight);
    border: none;
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}

#densitySlider::handle:horizontal:hover {
    background: palette(light);
}

/* ========================================
   SETUP WIZARD - Wizard-specific widget styling
   ======================================== */
#wizardHint {
    color: palette(text);
}

#wizardInfo {
    color: palette(text);
}

#wizardFieldDescription {
    color: palette(text);
}

#wizardAutoAuthInfo {
    color: palette(text);
}

#wizardHelpLink {
    /* link color handled by anchor tags */
}

#wizardDiskSpace {
    color: palette(text);
}

#wizardDiskSpace[diskWarning="warning"] {
    color: #e67e22;
}

#wizardDiskSpace[diskWarning="critical"] {
    color: #e74c3c;
}

#wizardThemeList {
    background-color: palette(base);
    border: 1px solid palette(dark);
}

#wizardThemeMeta {
    background-color: palette(base);
    border: 1px solid palette(dark);
    border-radius: 4px;
}

#wizardThemeAuthor {
    color: palette(text);
    font-weight: bold;
}

#wizardThemeDesc {
    color: palette(text);
}

#wizardPasswordToggle {
    background: transparent;
    border: 1px solid palette(mid);
    border-radius: 4px;
    padding: 2px;
}

#wizardPasswordToggle:checked {
    background-color: palette(highlight);
    border-color: palette(highlight);
}

/* ========================================
   SEMANTIC STATUS COLORS - Dynamic property-driven
   ======================================== */
QLabel[status="success"] { color: #27ae60; }
QLabel[status="error"]   { color: #e74c3c; }
QLabel[status="warning"] { color: #e67e22; }
QLabel[status="success"][fontWeight="bold"] { color: #27ae60; font-weight: bold; }
QLabel[status="error"][fontWeight="bold"]   { color: #e74c3c; font-weight: bold; }
QLabel[status="warning"][fontWeight="bold"] { color: #e67e22; font-weight: bold; }

/* Health indicator dots */
QLabel#healthDot { background: transparent; border-radius: 6px; }
QLabel#healthDot[health="green"]  { border: 2px solid #27ae60; }
QLabel#healthDot[health="yellow"] { border: 2px solid #f39c12; }
QLabel#healthDot[health="red"]    { border: 2px solid #e74c3c; }

/* Score badge colors (tag manager) */
QLabel#scoreBadge[scoreSign="positive"] { color: #28b43c; }
QLabel#scoreBadge[scoreSign="negative"] { color: #c83232; }

/* Loading overlay */
QWidget#loadingOverlay { background-color: rgba(0, 0, 0, 150); }

/* Loading overlay status label */
QLabel#loadingStatusLabel { font-weight: bold; }

/* Launch overlay */
QWidget#launchOverlay { background-color: rgba(0, 0, 0, 150); }
QLabel#launchOverlayHeader { font-size: 16pt; font-weight: bold; }
QLabel#launchOverlayTitle { font-weight: bold; }
QPushButton#launchButtonRunning { }

/* Game settings status colors */
QLabel#gameSettingsStatus[status="installed"]  { color: #27ae60; }
QLabel#gameSettingsStatus[status="detected"]   { color: #e67e22; }
QLabel#gameSettingsStatus[status="configured"] { color: #3498db; }
QLabel#gameSettingsStatus[status="available"]  { color: #3498db; }
QLabel#gameSettingsStatus[status="error"]      { color: #e74c3c; }

QLabel#inDevelopmentLabel {
    color: #cc0000;
    font-size: 18pt;
    font-weight: bold;
}

/* Sync dialog section headers */
QLabel#syncSectionLabel {
    font-weight: bold;
}

/* Collapsible section header (Settings) */
QPushButton#collapsibleHeader {
    text-align: left;
    font-weight: bold;
    padding: 6px 4px;
}
QPushButton#collapsibleHeader:hover {
    background: palette(midlight);
}

/* Metadata priority editor panels */
QWidget#prioritySidebarPanel {
    border: 1px solid palette(mid);
    border-radius: 3px;
}

QStackedWidget#priorityStack {
    border: 1px solid palette(mid);
    border-radius: 3px;
}

/* Metadata priority field buttons — explicit palette colors required because
   any QSS property causes Qt to switch from native to QSS rendering */
QPushButton#priorityFieldButton {
    text-align: left;
    padding: 4px 8px;
    background-color: palette(button);
    color: palette(button-text);
}

QPushButton#priorityFieldButton:hover {
    background-color: palette(midlight);
    border: 1px solid palette(highlight);
}

/* Back to About button on non-About tabs */
QPushButton#backToAboutButton {
    padding: 6px 16px;
}

/* ========================================
   CONFIG EDITOR DIALOG
   ======================================== */
QDialog#configEditor QPlainTextEdit {
    background-color: palette(base);
    color: palette(text);
    border: 1px solid palette(mid);
}
"""


MIN_ZOOM = 50
MAX_ZOOM = 400

# Default delegate config (preserves current behavior for all existing themes)
DEFAULT_DELEGATE_CONFIG = {
    "image_radius": 6,      # Corner radius for image clipping (covers/screenshots)
    "border_radius": 4,     # Corner radius for cover/screenshot background border
    "hover_border_width": 2, # Border width on hover
}


def get_themes_dir() -> Path:
    """Get themes directory path"""
    return get_config_dir() / "themes"


def ensure_themes_dir() -> Path:
    """Ensure themes directory exists"""
    themes_dir = get_themes_dir()
    themes_dir.mkdir(parents=True, exist_ok=True)
    return themes_dir


def get_bundled_themes_dir() -> Path:
    """Get bundled themes directory in package"""
    return Path(__file__).parent.parent / "assets" / "themes"


def _file_hash(path: Path) -> str:
    """Calculate SHA-256 hash of a file's contents."""
    hasher = hashlib.sha256()
    with open(path, 'rb') as f:
        hasher.update(f.read())
    return hasher.hexdigest()


def _migrate_old_theme_backups(themes_dir: Path) -> None:
    """Move old timestamped backup files to .backups subfolder.

    Migrates files like 'theme.qss.20260119_162234' from themes/ to themes/.backups/
    """
    backups_dir = themes_dir / ".backups"

    # Find old backup files (*.qss.YYYYMMDD_HHMMSS pattern)
    import re
    backup_pattern = re.compile(r'^.+\.qss\.\d{8}_\d{6}$')

    old_backups = [f for f in themes_dir.iterdir()
                   if f.is_file() and backup_pattern.match(f.name)]

    if old_backups:
        backups_dir.mkdir(exist_ok=True)
        for backup_file in old_backups:
            dest = backups_dir / backup_file.name
            try:
                shutil.move(str(backup_file), str(dest))
                logger.info(f"Migrated theme backup to .backups/: {backup_file.name}")
            except Exception as e:
                logger.warning(f"Failed to migrate backup {backup_file.name}: {e}")


def _migrate_renamed_themes(user_themes_dir: Path) -> None:
    """Move orphaned theme files from renamed bundled themes to .backups/.

    When bundled themes are renamed, old copies in the user's themes directory
    become orphaned (install_bundled_themes matches by filename). This migrates
    known old names so they don't clutter the theme picker.

    Also migrates the user's selected theme config if it pointed to an old name.
    """
    # Old filename stem → new filename stem
    RENAMED_THEMES = {
        "simon-the-sorcerer": "gilded-grimoire",
        "clair-obscur": "fin_de_siecle",
        "trails-beyond-the-horizon": "azure-horizon",
    }

    backups_dir = user_themes_dir / ".backups"
    migrated = False

    for old_stem, new_stem in RENAMED_THEMES.items():
        old_file = user_themes_dir / f"{old_stem}.luducat-theme"
        if not old_file.exists():
            continue

        backups_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{old_stem}.luducat-theme.{timestamp}"
        shutil.move(str(old_file), str(backups_dir / backup_name))
        logger.info(f"Migrated renamed theme to .backups/: {old_stem} → {new_stem}")
        migrated = True

        # Also clean up extracted assets from old package name
        old_assets = user_themes_dir / "assets" / old_stem
        if old_assets.is_dir():
            shutil.rmtree(old_assets, ignore_errors=True)
            logger.info(f"Removed old theme assets: assets/{old_stem}/")

    if not migrated:
        return

    # Migrate user's selected theme in config if it pointed to an old name
    try:
        from luducat.core.config import Config
        config = Config()
        selected = config.get("appearance.theme", "")
        for old_stem, new_stem in RENAMED_THEMES.items():
            if selected == f"package:{old_stem}":
                config.set("appearance.theme", f"package:{new_stem}")
                logger.info(
                    f"Migrated selected theme: package:{old_stem} → package:{new_stem}"
                )
                break
    except Exception as e:
        logger.warning(f"Could not migrate theme config: {e}")


def install_bundled_themes() -> int:
    """Install bundled themes from application package to user config.

    Copies themes from luducat/data/themes/ to ~/.config/luducat/themes/.
    - New themes are installed directly
    - Existing themes are compared by content hash; if different, the user's
      version is backed up to .backups/ subfolder before updating
    - Also installs variant JSON files to variants/ subdirectory

    Returns:
        Number of themes installed or updated
    """
    bundled_dir = get_bundled_themes_dir()
    if not bundled_dir.exists():
        logger.debug("No bundled themes directory found")
        return 0

    user_themes_dir = ensure_themes_dir()

    # Migrate any old backup files to .backups subfolder
    _migrate_old_theme_backups(user_themes_dir)

    # Migrate orphaned themes from renames to .backups/
    _migrate_renamed_themes(user_themes_dir)

    # Ensure .backups directory exists for new backups
    backups_dir = user_themes_dir / ".backups"

    installed_count = 0

    # Install QSS files (including base.qss template)
    for theme_file in bundled_dir.glob("*.qss"):
        dest_file = user_themes_dir / theme_file.name

        try:
            if not dest_file.exists():
                # New theme - just copy
                shutil.copy2(theme_file, dest_file)
                logger.info(f"Installed bundled theme: {theme_file.stem}")
                installed_count += 1
            else:
                # Existing theme - check if content differs
                bundled_hash = _file_hash(theme_file)
                user_hash = _file_hash(dest_file)

                if bundled_hash != user_hash:
                    # Content differs - backup user's version to .backups/
                    backups_dir.mkdir(exist_ok=True)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    backup_name = f"{theme_file.stem}.qss.{timestamp}"
                    backup_path = backups_dir / backup_name
                    shutil.copy2(dest_file, backup_path)
                    logger.info(f"Backed up user theme to .backups/: {backup_name}")

                    # Copy new bundled theme
                    shutil.copy2(theme_file, dest_file)
                    logger.info(f"Updated bundled theme: {theme_file.stem}")
                    installed_count += 1

        except Exception as e:
            logger.error(f"Failed to install theme {theme_file.name}: {e}")

    if installed_count > 0:
        logger.info(f"Installed/updated {installed_count} bundled theme(s)")

    # Install .luducat-theme package files
    package_count = 0
    for package_file in bundled_dir.glob("*.luducat-theme"):
        dest_file = user_themes_dir / package_file.name

        try:
            needs_install = False
            if not dest_file.exists():
                needs_install = True
            else:
                # Existing package - check if content differs
                bundled_hash = _file_hash(package_file)
                user_hash = _file_hash(dest_file)

                if bundled_hash != user_hash:
                    # Content differs - backup user's version to .backups/
                    backups_dir.mkdir(exist_ok=True)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    backup_name = f"{package_file.stem}.luducat-theme.{timestamp}"
                    backup_path = backups_dir / backup_name
                    shutil.copy2(dest_file, backup_path)
                    logger.info(f"Backed up user theme package to .backups/: {backup_name}")
                    needs_install = True

            if needs_install:
                # Copy the package file
                shutil.copy2(package_file, dest_file)
                logger.info(f"Installed theme package: {package_file.stem}")
                package_count += 1

                # Extract image assets from package to assets/<package-name>/ subdirectory
                import zipfile
                asset_extensions = {'.jpg', '.jpeg', '.png', '.svg', '.gif'}
                package_name = package_file.stem
                with zipfile.ZipFile(package_file, 'r') as zf:
                    for name in zf.namelist():
                        ext = Path(name).suffix.lower()
                        if ext in asset_extensions:
                            # Extract asset to assets/<package-name>/ subdir
                            assets_dir = user_themes_dir / "assets" / package_name
                            assets_dir.mkdir(parents=True, exist_ok=True)
                            # Validate member doesn't escape target directory
                            asset_dest = (assets_dir / name).resolve()
                            if not asset_dest.is_relative_to(assets_dir.resolve()):
                                logger.warning(
                                    f"Skipping theme asset with path traversal: {name}"
                                )
                                continue
                            asset_data = zf.read(name)
                            asset_dest.parent.mkdir(parents=True, exist_ok=True)
                            with open(asset_dest, 'wb') as f:
                                f.write(asset_data)
                            logger.info(f"Extracted theme asset: assets/{package_name}/{name}")

        except Exception as e:
            logger.error(f"Failed to install theme package {package_file.name}: {e}")

    if package_count > 0:
        logger.info(f"Installed/updated {package_count} theme package(s)")

    # Install variant JSON files
    bundled_variants_dir = bundled_dir / "variants"
    if bundled_variants_dir.exists():
        user_variants_dir = user_themes_dir / "variants"
        user_variants_dir.mkdir(exist_ok=True)

        variant_count = 0
        for variant_file in bundled_variants_dir.glob("*.json"):
            dest_file = user_variants_dir / variant_file.name

            try:
                if not dest_file.exists():
                    # New variant - just copy
                    shutil.copy2(variant_file, dest_file)
                    logger.info(f"Installed theme variant: {variant_file.stem}")
                    variant_count += 1
                else:
                    # Existing variant - check if content differs
                    bundled_hash = _file_hash(variant_file)
                    user_hash = _file_hash(dest_file)

                    if bundled_hash != user_hash:
                        # Update if bundled version changed
                        shutil.copy2(variant_file, dest_file)
                        logger.info(f"Updated theme variant: {variant_file.stem}")
                        variant_count += 1

            except Exception as e:
                logger.error(f"Failed to install variant {variant_file.name}: {e}")

        if variant_count > 0:
            logger.info(f"Installed/updated {variant_count} theme variant(s)")

    # Copy theme assets (images) alongside QSS files
    asset_extensions = {'.jpg', '.jpeg', '.png', '.svg'}
    for asset_file in bundled_dir.iterdir():
        if not asset_file.is_file():
            continue
        if asset_file.suffix.lower() not in asset_extensions:
            continue

        dest_file = user_themes_dir / asset_file.name
        try:
            if not dest_file.exists():
                # New asset - just copy
                shutil.copy2(asset_file, dest_file)
                logger.info(f"Installed theme asset: {asset_file.name}")
            else:
                # Existing asset - check if content differs
                bundled_hash = _file_hash(asset_file)
                user_hash = _file_hash(dest_file)

                if bundled_hash != user_hash:
                    # Update if bundled version changed
                    shutil.copy2(asset_file, dest_file)
                    logger.info(f"Updated theme asset: {asset_file.name}")
        except Exception as e:
            logger.error(f"Failed to install theme asset {asset_file.name}: {e}")

    return installed_count


class ThemeManager(QObject):
    """Manages application theming and scaling

    Supports both:
    - New variant-based themes (JSON color schemes + base template)
    - Legacy QSS themes (for backward compatibility)

    Signals:
        theme_changed: Emitted when theme is applied
        zoom_changed: Emitted when zoom level changes
    """

    theme_changed = Signal()
    zoom_changed = Signal(int)

    def __init__(self, app: QApplication, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.app = app
        self._current_theme = THEME_SYSTEM
        self._current_variant = "default"  # Current variant for variant-based themes
        self._current_zoom = DEFAULT_ZOOM

        # Save system font for reset during theme switching
        # This captures the font before any theme stylesheet modifies it
        self._system_font = QFont(app.font())  # Deep copy of system font

        self._base_font_size = app.font().pointSize()
        if self._base_font_size <= 0:
            self._base_font_size = 10  # Fallback

        # Delegate visual config (per-theme, read from theme.json delegate_config)
        self._delegate_config: Dict[str, int] = dict(DEFAULT_DELEGATE_CONFIG)

        # Per-theme colors (from variant JSON or package metadata)
        from .theme_variables import DEFAULT_VALUES
        self._fav_color: str = DEFAULT_VALUES["fav_color"]
        self._fav_star_color: str = DEFAULT_VALUES["fav_star_color"]
        self._download_completed: str = DEFAULT_VALUES["download_completed"]
        self._download_failed: str = DEFAULT_VALUES["download_failed"]
        self._download_paused: str = DEFAULT_VALUES["download_paused"]
        self._score_positive: str = DEFAULT_VALUES["score_positive"]
        self._score_negative: str = DEFAULT_VALUES["score_negative"]
        self._navbar_icon_color: str = DEFAULT_VALUES["navbar_icon_color"]

        # Cache for generated QSS from variants
        self._variant_qss_cache: Dict[str, str] = {}

    def get_available_themes(self) -> List[Dict[str, str]]:
        """Get list of available themes

        Returns list of themes in order:
        1. System theme
        2. Variant-based themes (from variants/*.json)
        3. Package themes (*.luducat-theme ZIP files)
        4. Legacy QSS themes (*.qss files without matching variant/package)

        Returns:
            List of dicts with 'id', 'name', and 'has_variants' keys
        """
        themes = [
            {"id": THEME_SYSTEM, "name": "System Standard", "has_variants": False}
        ]

        themes_dir = get_themes_dir()
        if not themes_dir.exists():
            return themes

        # Track names to avoid duplicates
        theme_names = set()

        # Get variant-based themes first (shared base template + color JSON)
        variants_dir = themes_dir / "variants"
        if variants_dir.exists():
            for json_file in sorted(variants_dir.glob("*.json")):
                variant_name = json_file.stem
                theme_names.add(variant_name.replace("_", "-").lower())
                display_name = self._get_variant_display_name(json_file)
                themes.append({
                    "id": f"variant:{variant_name}",
                    "name": display_name,
                    "has_variants": False
                })

        # Get package themes (.luducat-theme ZIP files)
        for package_file in sorted(themes_dir.glob("*.luducat-theme")):
            package_name = package_file.stem
            theme_names.add(package_name.replace("_", "-").lower())
            display_name = self._get_package_display_name(package_file)
            themes.append({
                "id": f"package:{package_name}",
                "name": display_name,
                "has_variants": False
            })

        # Add legacy QSS themes (ones without matching variant/package)
        for qss_file in sorted(themes_dir.glob("*.qss")):
            theme_id = qss_file.stem
            # Skip base.qss template
            if theme_id == "base":
                continue

            # Skip if there's already a variant or package with similar name
            normalized_name = theme_id.replace("_", "-").replace(" ", "-").lower()
            # Also check without common suffixes
            base_name = normalized_name.replace("-enhanced", "").replace("-se", "")

            if normalized_name in theme_names or base_name in theme_names:
                continue

            display_name = self._get_theme_display_name(qss_file)
            themes.append({
                "id": f"custom:{theme_id}",
                "name": f"{display_name} (Legacy)",
                "has_variants": False
            })

        # Pin flagship themes at top, rest alphabetical by name
        pinned = [
            THEME_SYSTEM,
            "package:luducat",
            "variant:luducat-classic",
            "variant:steam-client",
            "package:steam-2003",
            "variant:gog-galaxy",
        ]
        pinned_order = {tid: i for i, tid in enumerate(pinned)}

        def _sort_key(t):
            tid = t["id"]
            if tid in pinned_order:
                return (0, pinned_order[tid], "")
            return (1, 0, t["name"].lower())

        themes.sort(key=_sort_key)

        return themes

    def get_theme_variants(self, theme_id: str) -> List[Dict[str, str]]:
        """Get available variants for a theme.

        For variant-based themes, returns all available JSON variants.
        For legacy themes and system theme, returns empty list.

        Args:
            theme_id: Theme identifier

        Returns:
            List of dicts with 'id' and 'name' keys
        """
        if not theme_id.startswith("variant:"):
            return []

        # For now, variants are standalone (each JSON is a complete theme)
        # In future with .luducat-theme packages, this would return
        # multiple variants from within a package
        return []

    def _get_variant_display_name(self, json_path: Path) -> str:
        """Extract display name from variant JSON file.

        Reads the 'name' field from the JSON, falls back to filename.
        """
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if name := data.get("name"):
                    return name
        except Exception:
            pass

        # Fallback: beautify filename
        return json_path.stem.replace("_", " ").replace("-", " ").title()

    def _get_package_display_name(self, package_path: Path) -> str:
        """Extract display name from .luducat-theme package.

        Reads the 'name' field from theme.json inside the ZIP, falls back to filename.
        """
        import zipfile

        try:
            with zipfile.ZipFile(package_path, 'r') as zf:
                with zf.open("theme.json") as f:
                    data = json.load(f)
                    if name := data.get("name"):
                        return name
        except Exception:
            pass

        # Fallback: beautify filename
        return package_path.stem.replace("_", " ").replace("-", " ").title()

    def _load_package_qss(self, package_name: str) -> Optional[str]:
        """Load QSS from a .luducat-theme package.

        Also reads delegate_config from theme.json and updates self._delegate_config.

        Args:
            package_name: Name of the package (without .luducat-theme extension)

        Returns:
            QSS string from the package, or None if loading failed
        """
        import zipfile

        themes_dir = get_themes_dir()
        package_path = themes_dir / f"{package_name}.luducat-theme"

        if not package_path.exists():
            logger.error(f"Theme package not found: {package_path}")
            return None

        try:
            with zipfile.ZipFile(package_path, 'r') as zf:
                # Read delegate_config from theme.json if present
                try:
                    with zf.open("theme.json") as f:
                        metadata = json.loads(f.read().decode("utf-8"))
                    pkg_config = metadata.get("delegate_config", {})
                    self._delegate_config = dict(DEFAULT_DELEGATE_CONFIG)
                    for key in DEFAULT_DELEGATE_CONFIG:
                        if key in pkg_config:
                            self._delegate_config[key] = int(pkg_config[key])
                    # Read per-theme colors from package metadata (if present)
                    if "fav_color" in metadata:
                        self._fav_color = metadata["fav_color"]
                    for _key in ("fav_star_color", "download_completed",
                                 "download_failed", "download_paused",
                                 "score_positive", "score_negative",
                                 "navbar_icon_color"):
                        if _key in metadata:
                            setattr(self, f"_{_key}", metadata[_key])
                except Exception:
                    self._delegate_config = dict(DEFAULT_DELEGATE_CONFIG)

                # Load base.qss from package
                with zf.open("base.qss") as f:
                    return f.read().decode("utf-8")
        except Exception as e:
            logger.error(f"Failed to load theme package {package_name}: {e}")
            return None

    def _generate_qss_from_variant(self, variant_name: str) -> Optional[str]:
        """Generate QSS from a variant JSON file.

        Uses the base.qss template and processes it with color variables
        from the variant JSON.

        Args:
            variant_name: Name of the variant (without .json extension)

        Returns:
            Generated QSS string, or None if generation failed
        """
        # Check cache first
        if variant_name in self._variant_qss_cache:
            return self._variant_qss_cache[variant_name]

        from .theme_package import generate_qss_from_variant

        themes_dir = get_themes_dir()
        variant_path = themes_dir / "variants" / f"{variant_name}.json"

        if not variant_path.exists():
            logger.error(f"Variant file not found: {variant_path}")
            return None

        try:
            # Use bundled base template
            base_path = get_bundled_themes_dir() / "base.qss"
            qss = generate_qss_from_variant(variant_path, base_path)

            if qss:
                # Cache the result
                self._variant_qss_cache[variant_name] = qss
                return qss
            else:
                logger.error(f"Failed to generate QSS from variant: {variant_name}")
                return None

        except Exception as e:
            logger.error(f"Error generating QSS from variant {variant_name}: {e}")
            return None

    def clear_variant_cache(self) -> None:
        """Clear the variant QSS cache.

        Call this when variant files are modified to force regeneration.
        """
        self._variant_qss_cache.clear()
        logger.debug("Cleared variant QSS cache")

    def _get_theme_display_name(self, qss_path: Path) -> str:
        """Extract display name from QSS file

        Looks for: /* Theme: Display Name */
        Falls back to filename
        """
        try:
            with open(qss_path, 'r') as f:
                first_line = f.readline().strip()
                if first_line.startswith("/* Theme:") and first_line.endswith("*/"):
                    name = first_line[9:-2].strip()
                    if name:
                        return name
        except Exception:
            pass

        # Fallback: beautify filename
        return qss_path.stem.replace("_", " ").replace("-", " ").title()

    def get_theme_metadata(self, qss_path: Path) -> Dict[str, str]:
        """Extract metadata from QSS file comments.

        Uses _get_theme_display_name for the name, and parses
        @meta block for author and description:
            /* @meta
               Author: Name
               Description: Text
            */

        Returns dict with: name, author, description
        """
        import re

        # Get display name using the existing method
        metadata = {
            "name": self._get_theme_display_name(qss_path),
            "author": "",
            "description": ""
        }

        try:
            with open(qss_path, 'r') as f:
                content = f.read(4000)  # First 4KB should contain metadata

            # Parse /* @meta ... */
            if match := re.search(r'/\*\s*@meta\s+(.*?)\*/', content, re.DOTALL):
                meta_block = match.group(1)
                for line in meta_block.split('\n'):
                    line = line.strip()
                    if ':' in line:
                        key, val = line.split(':', 1)
                        key = key.strip().lower()
                        val = val.strip()
                        if key in ("author", "description"):
                            metadata[key] = val

        except Exception as e:
            logger.debug(f"Failed to read theme metadata from {qss_path}: {e}")

        return metadata

    def _resolve_theme_urls(self, qss_content: str, assets_dir: Optional[Path] = None) -> str:
        """Replace relative url() paths with absolute paths.

        Qt resolves url() relative to CWD, not the QSS file location.
        This method converts relative paths like url(filename.jpg) to
        absolute paths.

        Args:
            qss_content: The QSS stylesheet content
            assets_dir: Directory containing assets (defaults to themes dir)

        Returns:
            QSS content with resolved absolute paths
        """
        import re
        base_dir = assets_dir if assets_dir else get_themes_dir()

        def replace_url(match):
            path = match.group(1).strip('\'"')
            # Skip absolute paths, URLs, and Qt resources
            if path.startswith('/') or path.startswith('http') or path.startswith(':'):
                return match.group(0)
            # Convert relative path to absolute
            absolute_path = base_dir / path
            return f'url({absolute_path})'

        pattern = r'url\(([^)]+)\)'
        return re.sub(pattern, replace_url, qss_content)

    def _reset_to_system_font(self) -> None:
        """Reset application font to system default.

        Called before applying any theme to ensure fonts from previous
        themes don't persist when the new theme doesn't explicitly set them.
        """
        # Clear stylesheet first to remove any font overrides
        self.app.setStyleSheet("")
        # Reset to system font
        self.app.setFont(self._system_font)
        logger.debug("Reset application font to system default")

    def _sync_app_font(self, qss_content: str) -> None:
        """Sync QApplication font with font-family/font-size from QSS.

        QSS font rules only apply to widget rendering. Delegates and custom
        painters read from QApplication.font(), which stays at the system
        default unless explicitly set. This extracts the QSS font and calls
        app.setFont() so everything is consistent.
        """
        import re
        # Extract font-family and font-size from the QWidget {} block
        widget_block = re.search(r'QWidget\s*\{([^}]*)\}', qss_content)
        if not widget_block:
            return

        block = widget_block.group(1)
        family_m = re.search(r'font-family:\s*([^;]+)', block)
        size_m = re.search(r'font-size:\s*(\d+)pt', block)

        if not family_m and not size_m:
            return

        font = QFont(self.app.font())
        if family_m:
            # Find first available font from the stack
            from PySide6.QtGui import QFontDatabase
            available = set(QFontDatabase.families())
            raw = family_m.group(1).strip()
            for candidate in raw.split(","):
                name = candidate.strip().strip('"').strip("'")
                if name in available:
                    font.setFamily(name)
                    break
        if size_m:
            font.setPointSize(int(size_m.group(1)))

        self.app.setFont(font)
        logger.debug(f"Synced app font: {font.family()} {font.pointSize()}pt")

    def apply_theme(self, theme_id: str, variant: Optional[str] = None) -> bool:
        """Apply a theme

        Args:
            theme_id: Theme identifier (THEME_SYSTEM, "variant:name", or "custom:name")
            variant: Optional variant name (for future .luducat-theme packages)

        Returns:
            True if theme was applied successfully
        """
        self._current_theme = theme_id
        if variant:
            self._current_variant = variant

        # Always reset font to system default before applying new theme
        # This prevents fonts from previous themes persisting
        self._reset_to_system_font()

        # Reset delegate config and per-theme colors to defaults
        # (package themes override in _load_package_qss; variants below)
        self._delegate_config = dict(DEFAULT_DELEGATE_CONFIG)
        from .theme_variables import DEFAULT_VALUES
        self._fav_color = DEFAULT_VALUES["fav_color"]
        self._fav_star_color = DEFAULT_VALUES["fav_star_color"]
        self._download_completed = DEFAULT_VALUES["download_completed"]
        self._download_failed = DEFAULT_VALUES["download_failed"]
        self._download_paused = DEFAULT_VALUES["download_paused"]
        self._score_positive = DEFAULT_VALUES["score_positive"]
        self._score_negative = DEFAULT_VALUES["score_negative"]

        if theme_id == THEME_SYSTEM:
            # Reset to system palette
            self.app.setPalette(self.app.style().standardPalette())
            # Apply minimal embedded stylesheet for essential styling
            self.app.setStyleSheet(SYSTEM_DEFAULT_QSS)
            logger.info("Applied system theme with minimal styling")
            self.theme_changed.emit()
            return True

        elif theme_id.startswith("variant:"):
            # New variant-based theme
            variant_name = theme_id[8:]  # Remove "variant:" prefix
            qss_content = self._generate_qss_from_variant(variant_name)

            if not qss_content:
                logger.error(f"Failed to generate theme from variant: {variant_name}")
                return False

            # Read fav_color from the variant JSON
            try:
                from .theme_package import load_variant_from_file
                themes_dir = get_themes_dir()
                variant_path = themes_dir / "variants" / f"{variant_name}.json"
                vobj = load_variant_from_file(variant_path)
                if vobj:
                    complete = vobj.get_complete_variables()
                    dv = DEFAULT_VALUES
                    self._fav_color = complete.get("fav_color", dv["fav_color"])
                    self._fav_star_color = complete.get("fav_star_color", dv["fav_star_color"])
                    self._download_completed = complete.get(
                        "download_completed", dv["download_completed"])
                    self._download_failed = complete.get("download_failed", dv["download_failed"])
                    self._download_paused = complete.get("download_paused", dv["download_paused"])
                    self._score_positive = complete.get("score_positive", dv["score_positive"])
                    self._score_negative = complete.get("score_negative", dv["score_negative"])
                    self._navbar_icon_color = complete.get(
                        "navbar_icon_color", dv["navbar_icon_color"])
            except Exception:
                pass  # Keep default

            try:
                # Parse and apply palette from generated QSS
                palette = self._parse_palette(qss_content)
                if palette:
                    self.app.setPalette(palette)
                    logger.debug(f"Applied palette from variant: {variant_name}")

                # Resolve relative url() paths to absolute paths
                qss_content = self._resolve_theme_urls(qss_content)

                if logger.isEnabledFor(logging.DEBUG):
                    log_dir = get_config_dir() / "logs"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    qss_dump = log_dir / "debug-qss.txt"
                    qss_dump.write_text(qss_content)
                    logger.debug(f"Wrote processed QSS to {qss_dump}")

                self.app.setStyleSheet(qss_content)
                self._sync_app_font(qss_content)
                logger.info(f"Applied variant-based theme: {variant_name}")
                self.theme_changed.emit()
                return True

            except Exception as e:
                logger.error(f"Failed to apply variant theme {variant_name}: {e}")
                return False

        elif theme_id.startswith("package:"):
            # Theme package (.luducat-theme ZIP file)
            package_name = theme_id[8:]  # Remove "package:" prefix
            qss_content = self._load_package_qss(package_name)

            if not qss_content:
                logger.error(f"Failed to load theme package: {package_name}")
                return False

            try:
                # Parse and apply palette if defined
                palette = self._parse_palette(qss_content)
                if palette:
                    self.app.setPalette(palette)
                    logger.debug(f"Applied palette from package: {package_name}")

                # Resolve relative url() paths to package assets directory
                assets_dir = get_themes_dir() / "assets" / package_name
                qss_content = self._resolve_theme_urls(qss_content, assets_dir)

                self.app.setStyleSheet(qss_content)
                self._sync_app_font(qss_content)
                logger.info(f"Applied theme package: {package_name}")
                self.theme_changed.emit()
                return True

            except Exception as e:
                logger.error(f"Failed to apply theme package {package_name}: {e}")
                return False

        elif theme_id.startswith("custom:"):
            # Legacy QSS theme
            theme_name = theme_id[7:]  # Remove "custom:" prefix
            qss_path = get_themes_dir() / f"{theme_name}.qss"

            if not qss_path.exists():
                logger.error(f"Theme file not found: {qss_path}")
                return False

            try:
                with open(qss_path, 'r') as f:
                    qss_content = f.read()

                # Parse and apply palette if defined
                palette = self._parse_palette(qss_content)
                if palette:
                    self.app.setPalette(palette)
                    logger.debug(f"Applied palette from theme: {theme_name}")

                # Resolve relative url() paths to absolute paths
                qss_content = self._resolve_theme_urls(qss_content)

                self.app.setStyleSheet(qss_content)
                self._sync_app_font(qss_content)
                logger.info(f"Applied legacy theme: {theme_name}")
                self.theme_changed.emit()
                return True

            except Exception as e:
                logger.error(f"Failed to load theme {theme_name}: {e}")
                return False

        else:
            logger.warning(f"Unknown theme: {theme_id}")
            return False

    def get_current_variant(self) -> str:
        """Get current variant name."""
        return self._current_variant

    def get_delegate_config(self) -> Dict[str, int]:
        """Get current theme's delegate visual config.

        Returns dict with keys: image_radius, border_radius, hover_border_width.
        Values are ints representing pixel values used by grid view delegates.
        """
        return dict(self._delegate_config)

    def get_fav_color(self) -> str:
        """Get the current theme's favorite button color (QSS/border)."""
        return self._fav_color

    def get_fav_star_color(self) -> str:
        """Get the current theme's favorite star color (painted in delegates)."""
        return self._fav_star_color

    def get_navbar_icon_color(self) -> str:
        """Get the current theme's navbar icon tint color."""
        return self._navbar_icon_color

    def get_download_colors(self) -> Dict[str, str]:
        """Get download status colors for the current theme."""
        return {
            "completed": self._download_completed,
            "failed": self._download_failed,
            "paused": self._download_paused,
        }

    def get_score_colors(self) -> Dict[str, str]:
        """Get score tint colors for the current theme."""
        return {
            "positive": self._score_positive,
            "negative": self._score_negative,
        }

    def _parse_palette(self, qss_content: str) -> Optional[QPalette]:
        """Parse @palette section from QSS content and create QPalette.

        QSS files can define palette colors using a special comment block:
            /* @palette
               Window=#171A21
               Midlight=#1F364D
               ...
            */

        Args:
            qss_content: The full QSS file content

        Returns:
            QPalette with colors set, or None if no @palette section found
        """
        import re

        # Find @palette block
        match = re.search(r'/\*\s*@palette\s+(.*?)\*/', qss_content, re.DOTALL)
        if not match:
            return None

        palette_text = match.group(1)
        palette = QPalette()

        # Parse each line for Role=Color
        for line in palette_text.split('\n'):
            line = line.strip()
            if '=' in line:
                try:
                    role_name, color_value = line.split('=', 1)
                    role_name = role_name.strip()
                    color_value = color_value.strip()

                    # Get the palette role dynamically
                    role = getattr(QPalette.ColorRole, role_name, None)
                    if role is not None and color_value:
                        palette.setColor(role, QColor(color_value))
                except Exception as e:
                    logger.debug(f"Failed to parse palette line '{line}': {e}")

        return palette

    def get_current_theme(self) -> str:
        """Get current theme ID"""
        return self._current_theme

    def set_current_zoom(self, zoom_percent: int) -> None:
        """Set current zoom level (for tracking only)

        The actual zoom is applied via QT_SCALE_FACTOR before QApplication
        creation. This method just tracks the value for the settings dialog.

        Args:
            zoom_percent: Zoom level (50-200%)
        """
        self._current_zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom_percent))

    def apply_zoom(self, zoom_percent: int) -> None:
        """Mark that zoom level will change on restart

        UI zoom is applied via QT_SCALE_FACTOR environment variable before
        QApplication is created, so changes require a restart.

        Args:
            zoom_percent: Zoom level (50-200%)
        """
        zoom_percent = max(MIN_ZOOM, min(MAX_ZOOM, zoom_percent))
        old_zoom = self._current_zoom
        self._current_zoom = zoom_percent

        if old_zoom != zoom_percent:
            logger.info(f"Zoom changed to {zoom_percent}% (restart required)")
            self.zoom_changed.emit(zoom_percent)

    def get_current_zoom(self) -> int:
        """Get current zoom level"""
        return self._current_zoom

    def get_base_font_size(self) -> int:
        """Get the system's base font size for relative sizing.

        Used by delegates and other components that need to size fonts
        relative to the user's system font preference.

        Returns:
            Base font size in points (typically 10-13pt depending on system)
        """
        return self._base_font_size


def create_demo_theme() -> None:
    """Create a demo theme file with documentation

    Creates ~/.config/luducat/themes/demo_dark.qss
    """
    return  # dummy dont need it

