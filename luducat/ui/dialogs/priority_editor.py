# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# priority_editor.py

"""Priority editor dialog for metadata fields

Reusable dialog for editing the source priority order for a single metadata field.
Features drag-and-drop reordering, enable/disable checkboxes, and move buttons.
"""

import logging
from typing import List, Optional, Set

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from luducat.core.metadata_resolver import (
    FIELD_SOURCE_CAPABILITIES,
    SOURCE_LABELS,
    get_resolver,
)
from luducat.core.plugin_manager import PluginManager

logger = logging.getLogger(__name__)


class PriorityEditorDialog(QDialog):
    """Dialog for editing a single field's metadata source priority

    Features:
    - Drag-and-drop reordering of sources
    - Checkboxes to enable/disable sources
    - Move to Top/Up/Down/Bottom buttons
    - Reset to Default button
    - Validation: at least one source must be enabled

    Disabled plugins are shown but marked "(not enabled)" and cannot
    be dragged or checked.
    """

    def __init__(
        self,
        field_name: str,
        field_label: str,
        current_priority: List[str],
        enabled_plugins: Set[str],
        parent: Optional[QWidget] = None,
        authenticated_plugins: Optional[Set[str]] = None,
    ):
        """Initialize the priority editor dialog

        Args:
            field_name: Internal field name (e.g., "cover", "description")
            field_label: Human-readable field label (e.g., "Cover", "Description")
            current_priority: Current priority order (list of source names)
            enabled_plugins: Set of currently enabled plugin names
            parent: Parent widget
            authenticated_plugins: Set of plugins that are authenticated (subset of enabled)
                                  If None, assumes all enabled plugins are authenticated.
        """
        super().__init__(parent)

        self._field_name = field_name
        self._field_label = field_label
        self._enabled_plugins = enabled_plugins
        # If not specified, assume all enabled are authenticated
        self._authenticated_plugins = authenticated_plugins if authenticated_plugins is not None else enabled_plugins
        self._available_sources = FIELD_SOURCE_CAPABILITIES.get(field_name, [])
        self._default_priority = get_resolver().get_effective_defaults().get(
            field_name, []
        )

        # Build initial priority: current enabled, then current disabled, then any new sources
        self._initial_priority = self._build_full_priority(current_priority)

        self.setWindowTitle(_("Edit Priority: {field_label}").format(field_label=field_label))
        self.setMinimumSize(450, 400)
        self.resize(500, 450)

        self._setup_ui()

    def _build_full_priority(self, current_priority: List[str]) -> List[str]:
        """Build priority list from user's config sources only.

        Only includes sources that are in the user's config priority.
        Sources from capabilities that aren't in the config are NOT shown —
        the config is the single source of truth (seed-then-delete architecture).

        Args:
            current_priority: User's current priority list

        Returns:
            List of config sources, preserving order
        """
        result = []
        seen = set()

        # Only add sources from the user's config that are still available
        for source in current_priority:
            if source in self._available_sources and source not in seen:
                result.append(source)
                seen.add(source)

        return result

    def _setup_ui(self) -> None:
        """Create dialog UI"""
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Header description
        desc = QLabel(
            _("Drag to reorder sources, or use the buttons on the right.\n"
              "Checked sources are used in priority order (top = highest).\n"
              "Unchecked or disabled sources are skipped.")
        )
        desc.setWordWrap(True)
        desc.setObjectName("dialogDescription")
        layout.addWidget(desc)

        # Main content: list on left, buttons on right
        content = QHBoxLayout()
        content.setSpacing(12)

        # Priority list with drag-drop
        self._list = QListWidget()
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setAlternatingRowColors(True)
        self._list.itemChanged.connect(self._on_item_changed)
        self._list.currentRowChanged.connect(self._update_button_states)

        self._populate_list()
        content.addWidget(self._list, 1)

        # Reorder buttons
        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(8)
        btn_layout.addStretch()

        self._btn_top = QPushButton(_("Move to Top"))
        self._btn_top.setToolTip(_("Move selected source to highest priority"))
        self._btn_top.clicked.connect(self._move_to_top)
        btn_layout.addWidget(self._btn_top)

        self._btn_up = QPushButton(_("Move Up"))
        self._btn_up.setToolTip(_("Move selected source up one position"))
        self._btn_up.clicked.connect(self._move_up)
        btn_layout.addWidget(self._btn_up)

        self._btn_down = QPushButton(_("Move Down"))
        self._btn_down.setToolTip(_("Move selected source down one position"))
        self._btn_down.clicked.connect(self._move_down)
        btn_layout.addWidget(self._btn_down)

        self._btn_bottom = QPushButton(_("Move to Bottom"))
        self._btn_bottom.setToolTip(_("Move selected source to lowest priority"))
        self._btn_bottom.clicked.connect(self._move_to_bottom)
        btn_layout.addWidget(self._btn_bottom)

        btn_layout.addSpacing(20)

        self._btn_reset = QPushButton(_("Reset to Default"))
        self._btn_reset.setToolTip(_("Reset this field to default priority order"))
        self._btn_reset.clicked.connect(self._reset_to_default)
        btn_layout.addWidget(self._btn_reset)

        btn_layout.addStretch()
        content.addLayout(btn_layout)

        layout.addLayout(content)

        # Warning label (shown when validation fails)
        self._warning_label = QLabel(_("At least one source must be enabled!"))
        from luducat.utils.style_helpers import set_status_property
        set_status_property(self._warning_label, "error", bold=True)
        self._warning_label.setVisible(False)
        layout.addWidget(self._warning_label)

        # Dialog buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self._update_button_states()

    def _populate_list(self) -> None:
        """Populate list with sources.

        Only shows sources that are enabled AND authenticated.
        Disabled or unauthenticated plugins are hidden entirely —
        they remain in the config so they reappear when authenticated.
        """
        self._list.clear()

        for source in self._initial_priority:
            # Skip sources that aren't enabled or authenticated
            if source not in self._enabled_plugins:
                continue
            if source not in self._authenticated_plugins:
                continue

            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, source)

            label = SOURCE_LABELS.get(source, PluginManager.get_store_display_name(source))
            item.setText(label)

            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)

            self._list.addItem(item)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        """Handle item checkbox state change"""
        source = item.data(Qt.ItemDataRole.UserRole)

        # Don't allow checking disabled or unauthenticated plugins
        if source not in self._enabled_plugins:
            # Block signals to prevent recursion
            self._list.blockSignals(True)
            item.setCheckState(Qt.CheckState.Unchecked)
            self._list.blockSignals(False)

            QMessageBox.information(
                self,
                _("Plugin Not Enabled"),
                _("The '{plugin_name}' plugin is not enabled.\n\n"
                  "Enable it in Settings > Plugins to use it as a metadata source.").format(
                    plugin_name=SOURCE_LABELS.get(source, source))
            )
            return

        if source not in self._authenticated_plugins:
            # Block signals to prevent recursion
            self._list.blockSignals(True)
            item.setCheckState(Qt.CheckState.Unchecked)
            self._list.blockSignals(False)

            QMessageBox.information(
                self,
                _("Plugin Not Authenticated"),
                _("The '{plugin_name}' plugin requires authentication.\n\n"
                  "Configure credentials in Settings > Plugins to use it as a metadata source.").format(
                    plugin_name=SOURCE_LABELS.get(source, source))
            )
            return

        # Validate: at least one source must be enabled
        self._validate_selection()

    def _validate_selection(self) -> bool:
        """Check that at least one source is enabled and authenticated

        Returns:
            True if valid, False if no sources are available
        """
        has_valid = False
        for i in range(self._list.count()):
            item = self._list.item(i)
            source = item.data(Qt.ItemDataRole.UserRole)
            if (item.checkState() == Qt.CheckState.Checked and
                    source in self._authenticated_plugins):
                has_valid = True
                break

        self._warning_label.setVisible(not has_valid)
        return has_valid

    def _update_button_states(self) -> None:
        """Update move button enabled states based on selection"""
        row = self._list.currentRow()
        count = self._list.count()
        has_selection = row >= 0

        # Check if selected item is draggable (enabled AND authenticated)
        can_move = False
        if has_selection:
            item = self._list.item(row)
            source = item.data(Qt.ItemDataRole.UserRole)
            can_move = source in self._authenticated_plugins

        self._btn_top.setEnabled(can_move and row > 0)
        self._btn_up.setEnabled(can_move and row > 0)
        self._btn_down.setEnabled(can_move and row < count - 1)
        self._btn_bottom.setEnabled(can_move and row < count - 1)

    def _move_to_top(self) -> None:
        """Move selected item to top of list"""
        row = self._list.currentRow()
        if row > 0:
            item = self._list.takeItem(row)
            self._list.insertItem(0, item)
            self._list.setCurrentRow(0)

    def _move_up(self) -> None:
        """Move selected item up one position"""
        row = self._list.currentRow()
        if row > 0:
            item = self._list.takeItem(row)
            self._list.insertItem(row - 1, item)
            self._list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        """Move selected item down one position"""
        row = self._list.currentRow()
        if row < self._list.count() - 1:
            item = self._list.takeItem(row)
            self._list.insertItem(row + 1, item)
            self._list.setCurrentRow(row + 1)

    def _move_to_bottom(self) -> None:
        """Move selected item to bottom of list"""
        row = self._list.currentRow()
        if row < self._list.count() - 1:
            item = self._list.takeItem(row)
            self._list.insertItem(self._list.count(), item)
            self._list.setCurrentRow(self._list.count() - 1)

    def _reset_to_default(self) -> None:
        """Reset to default priority order"""
        self._initial_priority = self._build_full_priority(self._default_priority)
        self._populate_list()
        self._validate_selection()
        self._update_button_states()

    def _on_accept(self) -> None:
        """Handle OK button - validate and accept"""
        if not self._validate_selection():
            QMessageBox.warning(
                self,
                _("Invalid Configuration"),
                _("At least one source must be enabled.\n\n"
                  "Check at least one source to continue.")
            )
            return

        self.accept()

    def get_priority(self) -> List[str]:
        """Get the current priority order (authenticated and checked sources only)

        Returns:
            List of source names in priority order
        """
        result = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            source = item.data(Qt.ItemDataRole.UserRole)
            if (item.checkState() == Qt.CheckState.Checked and
                    source in self._authenticated_plugins):
                result.append(source)
        return result

    def get_full_order(self) -> List[str]:
        """Get the full ordering (all sources, for UI display)

        Returns:
            List of all source names in current order
        """
        result = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            result.append(item.data(Qt.ItemDataRole.UserRole))
        return result
