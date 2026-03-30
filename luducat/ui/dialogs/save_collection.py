# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# save_collection.py

"""Save Collection dialog for creating new collections from current filter state."""

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
)

from .tag_editor import ColorButton
from ...core.constants import GAME_MODE_FILTERS
from ...core.plugin_manager import PluginManager

logger = logging.getLogger(__name__)


class SaveCollectionDialog(QDialog):
    """Dialog for saving the current filter state as a collection.

    Returns the collection parameters via result(). Caller creates the
    actual collection via game_service.
    """

    def __init__(
        self,
        filters: Dict[str, Any],
        matched_count: int,
        parent=None,
    ):
        """
        Args:
            filters: Current filter dict from filter_bar.get_active_filters()
            matched_count: Number of games currently matching the filters
            parent: Parent widget
        """
        super().__init__(parent)
        self.setWindowTitle(_("Save Collection"))
        self.setMinimumWidth(450)

        self._filters = filters
        self._matched_count = matched_count
        self._color: Optional[str] = None

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Name
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel(_("Name:")))
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText(_("Collection name"))
        name_row.addWidget(self._name_input)
        layout.addLayout(name_row)

        # Color (optional)
        color_row = QHBoxLayout()
        color_row.addWidget(QLabel(_("Color:")))
        self._color_btn = ColorButton("#5c7cfa")
        self._color_btn.color_changed.connect(self._on_color_changed)
        color_row.addWidget(self._color_btn)
        color_label = QLabel(_("(optional)"))
        color_label.setObjectName("hintLabel")
        color_row.addWidget(color_label)
        color_row.addStretch()
        layout.addLayout(color_row)

        # Type selection
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel(_("Type:")))
        self._type_combo = QComboBox()
        self._type_combo.addItem(
            _("Smart filter (updates automatically)"), "dynamic"
        )
        self._type_combo.addItem(
            _("Fixed list ({count} games)").format(count=self._matched_count),
            "static",
        )
        type_row.addWidget(self._type_combo)
        layout.addLayout(type_row)

        # Notes
        notes_label = QLabel(_("Notes:"))
        notes_label.setObjectName("hintLabel")
        layout.addWidget(notes_label)
        self._notes_input = QPlainTextEdit()
        self._notes_input.setMaximumHeight(80)
        self._notes_input.setPlaceholderText(_("Optional description or notes"))
        layout.addWidget(self._notes_input)

        # Filter summary
        summary_label = QLabel(_("Current filters:"))
        summary_label.setObjectName("hintLabel")
        layout.addWidget(summary_label)

        summary_text = self._build_filter_summary()
        self._summary = QLabel(summary_text)
        self._summary.setWordWrap(True)
        self._summary.setObjectName("hintLabel")
        self._summary.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._summary)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._name_input.setFocus()

    def _on_color_changed(self, color: str) -> None:
        self._color = color

    def _on_accept(self) -> None:
        name = self._name_input.text().strip()
        if not name:
            self._name_input.setFocus()
            return
        self.accept()

    def get_result(self) -> Dict[str, Any]:
        """Return the collection parameters after dialog is accepted."""
        return {
            "name": self._name_input.text().strip(),
            "type": self._type_combo.currentData() or "dynamic",
            "color": self._color,
            "notes": self._notes_input.toPlainText().strip() or None,
        }

    def _build_filter_summary(self) -> str:
        """Build HTML bullet list of active filters."""
        lines = []
        f = self._filters

        # Base filter
        base = f.get("base_filter", "all")
        if base != "all":
            base_labels = {"recent": _("Recently Played"), "hidden": _("Hidden Games")}
            lines.append(base_labels.get(base, base))

        # Type filters
        type_labels = {
            "favorites": _("Favorites"), "free": _("Free Games"),
            "installed": _("Installed"), "demos": _("Demos"),
        }
        for t in f.get("type_filters", []):
            lines.append(type_labels.get(t, t))

        # Stores (only if not all)
        stores = f.get("stores", [])
        if stores:
            store_names = ", ".join(
                sorted(PluginManager.get_store_display_name(s) for s in stores)
            )
            lines.append(_("Stores: {stores}").format(stores=store_names))

        # Game modes
        for m in f.get("game_modes", []):
            lines.append(_(GAME_MODE_FILTERS.get(m, m)))

        # Developers
        devs = f.get("developers", [])
        if devs:
            lines.append(_("Developers: {devs}").format(devs=", ".join(sorted(devs))))

        # Publishers
        pubs = f.get("publishers", [])
        if pubs:
            lines.append(_("Publishers: {pubs}").format(pubs=", ".join(sorted(pubs))))

        # Genres
        genres = f.get("genres", [])
        if genres:
            lines.append(_("Genres: {genres}").format(genres=", ".join(sorted(genres))))

        # Tags
        tags = f.get("tags", [])
        if tags:
            lines.append(_("Tags: {tags}").format(tags=", ".join(sorted(tags))))

        # Years
        years = f.get("years", [])
        if years:
            lines.append(_("Years: {years}").format(years=", ".join(sorted(years))))

        # Boolean filters
        bool_labels = {
            "filter_family_shared": _("Family Shared"),
            "filter_orphaned": _("Unlinked Games"),
            "filter_private_apps": _("Private Apps"),
            "filter_delisted": _("Delisted"),
            "filter_protondb": _("ProtonDB Rated"),
            "filter_steam_deck": _("Steam Deck Verified"),
        }
        for key, label in bool_labels.items():
            if f.get(key, False):
                lines.append(label)

        if not lines:
            return "<i>" + _("No active filters (showing all games)") + "</i>"

        bullets = "".join(f"<br>• {line}" for line in lines)
        return bullets
