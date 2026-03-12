# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# developer_console.py

"""Developer Console dialog for luducat

Non-modal tabbed dialog accessible from the Tools menu:
- Log tab (always visible): real-time log viewer with level filter, search, copy, clear
- Network tab: per-plugin, per-domain request stats from NetworkManager
"""

import logging
import time
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.constants import APP_NAME
from ...core.logging import get_memory_handler

logger = logging.getLogger(__name__)

# Level filter mapping: display label → logging level (or None for "All")
_LEVEL_FILTERS = [
    (N_("All"), None),
    (N_("Debug"), logging.DEBUG),
    (N_("Info"), logging.INFO),
    (N_("Warning"), logging.WARNING),
    (N_("Error"), logging.ERROR),
]

# Auto-refresh intervals: label → milliseconds (0 = off)
_REFRESH_INTERVALS = [
    (N_("Off"), 0),
    ("1s", 1000),
    ("5s", 5000),
    ("10s", 10000),
    ("30s", 30000),
]


class _LogSignalBridge(QObject):
    """Thread-safe bridge: MemoryLogHandler callback → Qt signal."""

    new_record = Signal(object)


_NUMERIC_SORT_COLS = {2, 3, 4}


def _format_bytes(n: int) -> str:
    """Human-readable byte size."""
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def _format_relative_time(ts: float) -> str:
    """Format a Unix timestamp as relative time or HH:MM:SS."""
    if ts <= 0:
        return ""
    delta = time.time() - ts
    if delta < 0:
        delta = 0
    if delta < 60:
        return _("{n}s ago").format(n=int(delta))
    elif delta < 3600:
        return _("{n}m ago").format(n=int(delta / 60))
    elif delta < 86400:
        return _("{n}h ago").format(n=int(delta / 3600))
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _make_net_item(
    plugin: str, domain: str, count: int, nbytes: int, ts: float,
) -> QTreeWidgetItem:
    """Create a QTreeWidgetItem with numeric sort values as a Python attr."""
    item = QTreeWidgetItem([
        plugin, domain, str(count), _format_bytes(nbytes),
        _format_relative_time(ts),
    ])
    item._sort_values = {2: count, 3: nbytes, 4: ts}
    return item


class DeveloperConsoleDialog(QDialog):
    """Non-modal Developer Console with Log and Network tabs.

    Singleton — one instance kept alive on MainWindow, shown/hidden.
    """

    def __init__(
        self,
        config=None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._config = config

        self.setWindowTitle(f"{APP_NAME} — {_('Developer Console')}")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        # Signal bridge for live log updates
        self._bridge = _LogSignalBridge(self)
        self._bridge.new_record.connect(self._on_new_record)

        # Auto-refresh timer for network tab
        self._net_refresh_timer = QTimer(self)
        self._net_refresh_timer.timeout.connect(self._refresh_network_stats)

        self._setup_ui()
        self._restore_geometry()

    # ── UI setup ─────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        # Log tab (always present)
        self._setup_log_tab()

        # Network tab — always visible so users can verify network activity
        self._setup_network_tab()

    def _setup_log_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)

        # Controls row
        controls = QHBoxLayout()

        # Level filter
        controls.addWidget(QLabel(_("Level:")))
        self._level_combo = QComboBox()
        for label, _level in _LEVEL_FILTERS:
            self._level_combo.addItem(_(label), _level)
        self._level_combo.currentIndexChanged.connect(self._refresh_log_view)
        controls.addWidget(self._level_combo)

        controls.addSpacing(12)

        # Search box
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText(_("Search..."))
        self._search_box.setClearButtonEnabled(True)
        self._search_box.textChanged.connect(self._refresh_log_view)
        controls.addWidget(self._search_box, 1)

        controls.addSpacing(12)

        # Copy button
        btn_copy = QPushButton(_("Copy"))
        btn_copy.setToolTip(_("Copy visible log text to clipboard"))
        btn_copy.clicked.connect(self._copy_log)
        controls.addWidget(btn_copy)

        # Clear button
        btn_clear = QPushButton(_("Clear"))
        btn_clear.setToolTip(_("Clear the log display"))
        btn_clear.clicked.connect(self._clear_log)
        controls.addWidget(btn_clear)

        layout.addLayout(controls)

        # Log text area
        self._log_text = QPlainTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._log_text.setMaximumBlockCount(10000)
        font = self._log_text.font()
        font.setFamily("monospace")
        self._log_text.setFont(font)
        layout.addWidget(self._log_text)

        self._tabs.addTab(tab, _("Log"))

    def _setup_network_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)

        # Controls row
        controls = QHBoxLayout()

        # Search filter
        self._net_search = QLineEdit()
        self._net_search.setPlaceholderText(_("Filter..."))
        self._net_search.setClearButtonEnabled(True)
        self._net_search.textChanged.connect(self._apply_network_filter)
        controls.addWidget(self._net_search, 1)

        controls.addSpacing(12)

        # Auto-refresh combo
        controls.addWidget(QLabel(_("Auto-refresh:")))
        self._net_refresh_combo = QComboBox()
        for label, ms in _REFRESH_INTERVALS:
            self._net_refresh_combo.addItem(_(label), ms)
        self._net_refresh_combo.currentIndexChanged.connect(
            self._on_net_refresh_interval_changed
        )
        controls.addWidget(self._net_refresh_combo)

        controls.addSpacing(12)

        # Reset button
        btn_reset = QPushButton(_("Reset"))
        btn_reset.setToolTip(_("Reset all network statistics"))
        btn_reset.clicked.connect(self._reset_network_stats)
        controls.addWidget(btn_reset)

        # Manual refresh button
        btn_refresh = QPushButton(_("Refresh"))
        btn_refresh.setToolTip(_("Refresh network statistics"))
        btn_refresh.clicked.connect(self._refresh_network_stats)
        controls.addWidget(btn_refresh)

        layout.addLayout(controls)

        # Tree widget
        self._net_tree = QTreeWidget()
        self._net_tree.setHeaderLabels([
            _("Plugin"),
            _("Domain"),
            _("Requests"),
            _("Data"),
            _("Last Request"),
        ])
        # Sorting is handled manually in _sort_network_tree() — never use
        # setSortingEnabled(True) as it triggers C++ operator< which
        # segfaults in PySide6/shiboken when QTreeWidgetItem.__lt__ is
        # overridden (even indirectly via subclass).
        self._net_tree.setSortingEnabled(False)
        self._net_tree.setRootIsDecorated(True)
        self._net_tree.setAlternatingRowColors(True)
        self._net_sort_col = 0
        self._net_sort_order = Qt.SortOrder.AscendingOrder

        header = self._net_tree.header()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        header.sectionClicked.connect(self._sort_network_tree)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        layout.addWidget(self._net_tree)

        self._tabs.addTab(tab, _("Network"))

    # ── Log tab logic ────────────────────────────────────────────────

    def _get_min_level(self) -> int:
        """Return the minimum log level from the combo filter, or 0 for All."""
        level = self._level_combo.currentData()
        return level if level is not None else 0

    def _passes_filter(self, record: logging.LogRecord) -> bool:
        """Check if a record passes the current level + search filters."""
        min_level = self._get_min_level()
        if min_level and record.levelno < min_level:
            return False
        search = self._search_box.text()
        if search:
            formatted = self._format_record(record)
            if search.lower() not in formatted.lower():
                return False
        return True

    @staticmethod
    def _format_record(record: logging.LogRecord) -> str:
        """Format a LogRecord for display (uses the handler's formatter)."""
        handler = get_memory_handler()
        if handler and handler.formatter:
            return handler.formatter.format(record)
        return record.getMessage()

    def _refresh_log_view(self) -> None:
        """Rebuild the log text from the memory buffer with current filters."""
        handler = get_memory_handler()
        if not handler:
            return

        lines = []
        for record in handler.records:
            if self._passes_filter(record):
                lines.append(self._format_record(record))

        self._log_text.setPlainText("\n".join(lines))

        # Scroll to bottom
        scrollbar = self._log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_new_record(self, record: logging.LogRecord) -> None:
        """Handle a new log record arriving while the dialog is visible."""
        if not self._passes_filter(record):
            return

        # Append to text widget (avoids full rebuild)
        self._log_text.appendPlainText(self._format_record(record))

    def _copy_log(self) -> None:
        """Copy visible log text to clipboard."""
        text = self._log_text.toPlainText()
        if text:
            clipboard = QApplication.clipboard()
            if clipboard:
                clipboard.setText(text)

    def _clear_log(self) -> None:
        """Clear the log display (buffer is preserved)."""
        self._log_text.clear()

    # ── Network tab logic ────────────────────────────────────────────

    def _refresh_network_stats(self) -> None:
        """Rebuild the network tree from NetworkManager stats."""
        try:
            from ...core.network_manager import get_network_manager
            nm = get_network_manager()
        except Exception:
            return

        if nm is None:
            return

        all_stats: Dict[str, Dict[str, Dict[str, Any]]] = nm.get_all_stats()

        # Save UI state before rebuild (None on first build)
        expanded, selected = self._save_net_tree_state()
        first_build = not expanded

        self._net_tree.clear()

        for plugin_name, domain_stats in sorted(all_stats.items()):
            if not domain_stats:
                # Show registered plugin even without stats
                plugin_item = _make_net_item(plugin_name, "", 0, 0, 0)
                self._net_tree.addTopLevelItem(plugin_item)
                continue

            # Group domains by parent (e.g. images-1.gog.com → *.gog.com)
            groups: Dict[str, list] = {}  # parent → [(domain, stats)]
            for domain, stats in domain_stats.items():
                parts = domain.split(".")
                if len(parts) >= 3:
                    parent = "*." + ".".join(parts[-2:])
                else:
                    parent = domain
                groups.setdefault(parent, []).append((domain, stats))

            # Plugin-level totals
            total_count = sum(s["count"] for s in domain_stats.values())
            total_bytes = sum(s["bytes"] for s in domain_stats.values())
            latest_ts = max(
                (s["last_request"] for s in domain_stats.values()), default=0
            )

            plugin_item = _make_net_item(
                plugin_name, "", total_count, total_bytes, latest_ts,
            )

            for parent_domain, entries in sorted(groups.items()):
                if len(entries) == 1:
                    # Single (sub)domain — add directly under plugin
                    domain, stats = entries[0]
                    child = _make_net_item(
                        domain, "",
                        stats["count"], stats["bytes"], stats["last_request"],
                    )
                    plugin_item.addChild(child)
                else:
                    # Multiple subdomains — group under parent
                    group_count = sum(s["count"] for _, s in entries)
                    group_bytes = sum(s["bytes"] for _, s in entries)
                    group_ts = max(
                        (s["last_request"] for _, s in entries), default=0
                    )
                    group_item = _make_net_item(
                        parent_domain, "",
                        group_count, group_bytes, group_ts,
                    )
                    for domain, stats in sorted(entries):
                        child = _make_net_item(
                            domain, "",
                            stats["count"], stats["bytes"],
                            stats["last_request"],
                        )
                        group_item.addChild(child)
                    plugin_item.addChild(group_item)

            self._net_tree.addTopLevelItem(plugin_item)
            if first_build:
                plugin_item.setExpanded(True)

        # Sort with current column/order, then apply search filter
        self._sort_network_tree(self._net_sort_col, toggle=False)
        self._apply_network_filter()

        # Restore UI state after rebuild
        self._restore_net_tree_state(expanded, selected)

    @staticmethod
    def _item_path(item: QTreeWidgetItem) -> str:
        """Build a stable identity key for a tree item (plugin/domain)."""
        parts = []
        node = item
        while node is not None:
            parts.append(node.text(0) or node.text(1))
            node = node.parent()
        return "/".join(reversed(parts))

    def _save_net_tree_state(self):
        """Capture expanded paths and selected item before a rebuild."""
        expanded = set()
        selected = None

        sel_items = self._net_tree.selectedItems()
        if sel_items:
            selected = self._item_path(sel_items[0])

        def _walk(item):
            if item.isExpanded():
                expanded.add(self._item_path(item))
            for i in range(item.childCount()):
                _walk(item.child(i))

        for i in range(self._net_tree.topLevelItemCount()):
            _walk(self._net_tree.topLevelItem(i))

        return expanded, selected

    def _restore_net_tree_state(self, expanded, selected):
        """Re-apply expanded paths and selection after a rebuild."""
        def _walk(item):
            path = self._item_path(item)
            if path in expanded:
                item.setExpanded(True)
            if path == selected:
                item.setSelected(True)
            for i in range(item.childCount()):
                _walk(item.child(i))

        for i in range(self._net_tree.topLevelItemCount()):
            _walk(self._net_tree.topLevelItem(i))

    def _reset_network_stats(self) -> None:
        """Reset all network statistics and refresh the display."""
        try:
            from ...core.network_manager import get_network_manager
            nm = get_network_manager()
        except Exception:
            return
        if nm is not None:
            nm.reset_all_stats()
        self._refresh_network_stats()

    def _apply_network_filter(self) -> None:
        """Show/hide tree items based on the network search box."""
        if not hasattr(self, "_net_tree"):
            return

        search = self._net_search.text().strip().lower() if hasattr(self, "_net_search") else ""

        for i in range(self._net_tree.topLevelItemCount()):
            plugin_item = self._net_tree.topLevelItem(i)
            plugin_match = not search or search in plugin_item.text(0).lower()

            any_child_visible = False
            for j in range(plugin_item.childCount()):
                child = plugin_item.child(j)
                child_text = child.text(0).lower() + " " + child.text(1).lower()
                child_match = not search or search in child_text

                # Check grandchildren too (subdomain groups)
                any_grandchild_visible = False
                for k in range(child.childCount()):
                    grandchild = child.child(k)
                    gc_text = grandchild.text(0).lower()
                    gc_match = not search or search in gc_text or child_match
                    grandchild.setHidden(not gc_match and not plugin_match)
                    if gc_match or plugin_match:
                        any_grandchild_visible = True

                visible = plugin_match or child_match or any_grandchild_visible
                child.setHidden(not visible)
                if visible:
                    any_child_visible = True

            plugin_item.setHidden(not plugin_match and not any_child_visible)

    def _sort_network_tree(self, column: int, toggle: bool = True) -> None:
        """Sort top-level items and their children in pure Python.

        PySide6/shiboken segfaults when QTreeWidgetItem.__lt__ is
        overridden in Python — any C++ sort that calls back into a
        Python __lt__ crashes.  We avoid this entirely by keeping
        setSortingEnabled(False) and sorting items manually.

        Args:
            column: Column index to sort by.
            toggle: If True (header click), toggle order on same column.
                    If False (refresh), re-apply current order.
        """
        if toggle:
            if column == self._net_sort_col:
                self._net_sort_order = (
                    Qt.SortOrder.DescendingOrder
                    if self._net_sort_order == Qt.SortOrder.AscendingOrder
                    else Qt.SortOrder.AscendingOrder
                )
            else:
                self._net_sort_col = column
                self._net_sort_order = Qt.SortOrder.AscendingOrder

        header = self._net_tree.header()
        header.setSortIndicator(column, self._net_sort_order)

        reverse = self._net_sort_order == Qt.SortOrder.DescendingOrder

        def sort_key(item):
            sv = getattr(item, "_sort_values", {})
            if column in _NUMERIC_SORT_COLS and column in sv:
                return sv[column]
            return item.text(column).lower()

        # Sort top-level items
        items = []
        while self._net_tree.topLevelItemCount():
            items.append(self._net_tree.takeTopLevelItem(0))
        items.sort(key=sort_key, reverse=reverse)
        for item in items:
            self._net_tree.addTopLevelItem(item)
            item.setExpanded(True)

            # Sort children within each top-level item
            children = []
            while item.childCount():
                children.append(item.takeChild(0))
            children.sort(key=sort_key, reverse=reverse)
            for child in children:
                item.addChild(child)

                # Sort grandchildren (subdomain groups)
                grandchildren = []
                while child.childCount():
                    grandchildren.append(child.takeChild(0))
                if grandchildren:
                    grandchildren.sort(key=sort_key, reverse=reverse)
                    for gc in grandchildren:
                        child.addChild(gc)

    def _on_net_refresh_interval_changed(self, index: int) -> None:
        """Start/stop auto-refresh timer based on combo selection."""
        ms = self._net_refresh_combo.currentData()
        self._net_refresh_timer.stop()
        if ms and ms > 0:
            self._net_refresh_timer.start(ms)

    # ── Show / hide lifecycle ────────────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)

        # Bulk-load log from buffer
        self._refresh_log_view()

        # Connect live callback
        handler = get_memory_handler()
        if handler:
            handler.set_callback(self._bridge.new_record.emit)

        # Refresh network stats and start auto-refresh if configured
        if hasattr(self, "_net_tree"):
            self._refresh_network_stats()
            # Re-arm auto-refresh timer if an interval was selected
            if hasattr(self, "_net_refresh_combo"):
                ms = self._net_refresh_combo.currentData()
                if ms and ms > 0:
                    self._net_refresh_timer.start(ms)

    def hideEvent(self, event) -> None:
        # Disconnect live callback
        handler = get_memory_handler()
        if handler:
            handler.set_callback(None)

        # Stop auto-refresh
        self._net_refresh_timer.stop()

        self._save_geometry()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        """Hide instead of destroy on close."""
        self._save_geometry()
        handler = get_memory_handler()
        if handler:
            handler.set_callback(None)
        self._net_refresh_timer.stop()
        self.hide()
        event.ignore()

    # ── Geometry persistence ─────────────────────────────────────────

    def _save_geometry(self) -> None:
        if self._config:
            geo = self.geometry()
            self._config.set("developer_console.x", geo.x())
            self._config.set("developer_console.y", geo.y())
            self._config.set("developer_console.width", geo.width())
            self._config.set("developer_console.height", geo.height())

    def _restore_geometry(self) -> None:
        if not self._config:
            self.resize(800, 500)
            return

        w = self._config.get("developer_console.width", 800)
        h = self._config.get("developer_console.height", 500)
        self.resize(w, h)

        x = self._config.get("developer_console.x", None)
        y = self._config.get("developer_console.y", None)
        if x is not None and y is not None:
            self.move(x, y)
