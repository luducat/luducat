# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# filter_bar.py

"""Filter bar for luducat

Contains:
- Filter dropdown (base filter, type filters, store filters)
- Quick filter buttons (All, Favorites)
- Tag filters (user-created tags)
"""

import logging
from typing import Dict, List, Optional, Set

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
    QMenu,
    QMessageBox,
)

from ..core.constants import (
    FILTER_BASE_ALL,
    FILTER_BASE_RECENT,
    FILTER_TYPE_FREE,
    FILTER_TYPE_FAVORITES,
    FILTER_TYPE_INSTALLED,
    FILTER_TYPE_DEMOS,
    SORT_MODE_NAME,
    SORT_MODE_RECENT,
    SORT_MODE_ADDED,
    SORT_MODE_PUBLISHER,
    SORT_MODE_DEVELOPER,
    SORT_MODE_RELEASE,
    SORT_MODE_FRANCHISE,
    SORT_MODE_FAMILY_LICENSES,
    GAME_MODE_FILTERS,
    DEFAULT_TAG_COLOR,
)
from ..core.plugin_manager import PluginManager
from ..utils.icons import load_tinted_icon

from PySide6.QtCore import QSortFilterProxyModel
from PySide6.QtGui import QCursor, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QListView,
    QSpinBox,
    QVBoxLayout,
)
from datetime import datetime

logger = logging.getLogger(__name__)


class FilterChip(QPushButton):
    """Toggleable filter chip button"""

    def __init__(self, text: str, chip_id: str, parent: Optional[QWidget] = None):
        super().__init__(text, parent)

        self.chip_id = chip_id
        self.setCheckable(True)
        self.setObjectName("filterChip")


class TagChip(FilterChip):
    """Tag filter chip with custom color"""

    def __init__(
        self,
        tag_name: str,
        color: str = DEFAULT_TAG_COLOR,
        source: str = "native",
        source_colors_enabled: bool = False,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(tag_name, f"tag:{tag_name}", parent)

        self.tag_name = tag_name
        self.tag_color = color
        self.tag_source = source
        self._source_colors_enabled = source_colors_enabled
        self.setObjectName("tagChip")
        self._update_style()

    @staticmethod
    def _contrast_text(hex_color: str) -> str:
        """Return 'white' or 'black' for best contrast against hex_color."""
        h = hex_color.lstrip('#')
        if len(h) < 6:
            return "white"
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return "black" if (0.299 * r + 0.587 * g + 0.114 * b) > 128 else "white"

    def _update_style(self) -> None:
        """Update chip style with tag color accent"""
        from ..core.constants import TAG_SOURCE_COLORS
        checked_text = self._contrast_text(self.tag_color)

        # Source color accent: 3px left border in brand color
        source_border = ""
        if self._source_colors_enabled and self.tag_source != "native":
            brand = TAG_SOURCE_COLORS.get(self.tag_source)
            if brand:
                source_border = f"border-left: 3px solid {brand};"

        self.setStyleSheet(f"""
            QPushButton#tagChip {{
                background: {self.tag_color}40;
                color: palette(text);
                border: 1px solid {self.tag_color};
                {source_border}
                padding: 4px 8px;
                border-radius: 3px;
            }}
            QPushButton#tagChip:checked {{
                background: {self.tag_color};
                color: {checked_text};
                {source_border}
            }}
            QPushButton#tagChip:hover {{
                background: {self.tag_color}60;
            }}
        """)

    def set_color(self, color: str) -> None:
        """Update tag color"""
        self.tag_color = color
        self._update_style()


class FilterCrumb(QFrame):
    """Removable filter indicator chip.

    Distinct from QPushButton — has its own QSS objectName and independent
    sizing/styling. Click anywhere to remove (no x button).

    Visual anatomy:
    [prefix] [label text]
    [_____ bottom border _____]

    Types:
    - "filter": metadata filter chips (dimmed accent bottom border)
    - "tag": tag chips (accent bottom border, * prefix)
    - "special": special tags (gold bottom border)
    """
    clicked = Signal()

    def __init__(
        self,
        label: str,
        crumb_type: str = "filter",
        *,
        color: str = "",
        tooltip_detail: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setObjectName("filterCrumb")
        self.crumb_type = crumb_type
        self.crumb_color = color

        # Set QSS property for type-based styling
        self.setProperty("crumbType", crumb_type)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(2)

        # Build display text with prefix
        if crumb_type == "tag":
            display_text = f"* {label}"
        elif crumb_type == "special":
            display_text = f"! {label}"
        else:
            display_text = label

        self._label = QLabel(display_text)
        self._label.setObjectName("filterCrumbLabel")
        layout.addWidget(self._label)

        # Cursor
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # Tooltip
        tooltip_lines = []
        if crumb_type == "tag":
            tooltip_lines.append(_("Tag: {label}").format(label=label))
        elif crumb_type == "special":
            tooltip_lines.append(_("Special: {label}").format(label=label))
        else:
            tooltip_lines.append(label)
        if tooltip_detail:
            tooltip_lines.append(tooltip_detail)
        tooltip_lines.append(_("Click to remove"))
        self.setToolTip("\n".join(tooltip_lines))

        # Apply bottom border color
        self._apply_bottom_border()

    def _apply_bottom_border(self) -> None:
        """Apply colored bottom border based on crumb type."""
        if self.crumb_color:
            border_color = self.crumb_color
        elif self.crumb_type == "special":
            border_color = self.palette().highlight().color().name()
        else:
            # Default: let QSS handle it (no inline override)
            return
        self.setStyleSheet(
            f"QFrame#filterCrumb {{ border-bottom: 2px solid {border_color}; }}"
        )

    def mousePressEvent(self, event) -> None:
        """Emit clicked on any mouse press."""
        self.clicked.emit()
        super().mousePressEvent(event)


class FilterBar(QWidget):
    """Filter bar widget

    Layout:
    [ Filter ▼ ] [All] [Favorites] | [ Sort: Name ▼ ] | Tags: ...

    Signals:
        filters_changed: Emitted when any filter changes
            Dict with keys: base_filter, type_filters, stores, show_hidden, tags
        sort_changed: Emitted when sort options change (mode, reverse, favorites_first)
        add_tag_requested: Emitted when user clicks "+ Add" tag button
    """

    filters_changed = Signal(dict)
    sort_changed = Signal(str, bool, bool)  # mode, reverse, favorites_first
    add_tag_requested = Signal()
    random_game_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("filterBarWidget")

        # Filter state
        self._base_filter = FILTER_BASE_ALL  # "all", "recent", or "hidden"
        self._type_filters: Set[str] = set()  # favorites, free
        self._active_stores: Set[str] = set()
        self._active_tags: Set[str] = set()
        self._active_game_modes: Set[str] = set()  # IGDB game mode names
        self._active_developers: Set[str] = set()
        self._active_publishers: Set[str] = set()
        self._active_genres: Set[str] = set()
        self._active_years: Set[str] = set()  # year strings like "2024"
        self._filter_family_shared: bool = False
        self._filter_orphaned: bool = False
        self._filter_protondb: bool = False
        self._filter_steam_deck: bool = False
        self._exact_stores_filter: bool = False

        # Sort state
        self._current_sort_mode = SORT_MODE_NAME
        self._sort_reverse = False
        self._favorites_first = False

        # Available stores (for minimum-one validation)
        self._available_stores: List[str] = []

        # UI references
        self._store_actions: Dict[str, QAction] = {}
        self._game_mode_actions: Dict[str, QAction] = {}
        self._available_tag_list: List[tuple[str, str, str]] = []  # (name, color, source)
        self._quick_tags: List[Dict] = []  # Quick-access tags (scored + frequent)
        self._source_colors_enabled: bool = False  # Show brand color accents
        self._available_developers: List[str] = []
        self._available_publishers: List[str] = []
        self._available_genres: List[str] = []
        self._available_years: List[str] = []

        self._setup_ui()

    def _setup_ui(self) -> None:
        """Create filter bar layout

        Controls row (Filter▼, All, Favorites, Sort▼, 🎲) is built as a
        detachable widget that gets embedded into the Toolbar via
        get_controls_widget() + Toolbar.embed_filter_controls().

        This widget contains only the crumb bar (active filter chips).
        """
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 2, 8, 4)
        main_layout.setSpacing(2)

        # --- Controls row (detachable — embedded into Toolbar) ---
        self._controls_widget = QWidget()
        controls_layout = QHBoxLayout(self._controls_widget)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(8)

        # Filter dropdown button
        self.btn_filter = QPushButton(_("Filter"))
        self.btn_filter.setObjectName("filterDropdownButton")
        self.btn_filter.setToolTip(_("Filter games by store, tag, genre, and more"))
        self._setup_filter_menu()
        controls_layout.addWidget(self.btn_filter)

        # Quick filter buttons
        self.btn_all = FilterChip(_("All"), FILTER_BASE_ALL)
        self.btn_all.setToolTip(_("Show all games"))
        self.btn_all.setChecked(True)
        self.btn_all.clicked.connect(self._on_all_clicked)
        controls_layout.addWidget(self.btn_all)

        self.btn_favorites = FilterChip(_("Favorites"), FILTER_TYPE_FAVORITES)
        self.btn_favorites.setToolTip(_("Show only favorite games"))
        self.btn_favorites.clicked.connect(self._on_favorites_clicked)
        controls_layout.addWidget(self.btn_favorites)

        # Separator before sort
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setFrameShadow(QFrame.Shadow.Sunken)
        controls_layout.addWidget(sep1)

        # Sort dropdown button
        self.btn_sort = QPushButton(_("Sort: {mode}").format(mode=_("Name")))
        self.btn_sort.setObjectName("sortButton")
        self.btn_sort.setToolTip(_("Change how games are sorted"))
        self._setup_sort_menu()
        controls_layout.addWidget(self.btn_sort)

        # Random game button (dice icon, square, matches sort button height)
        self.btn_random = QPushButton()
        self.btn_random.setObjectName("randomButton")
        self.btn_random.setToolTip(_("Pick a random game"))
        h = self.btn_sort.sizeHint().height()
        self.btn_random.setFixedSize(h, h)
        self.btn_random.clicked.connect(self.random_game_requested.emit)
        self._update_dice_icon()
        controls_layout.addWidget(self.btn_random)

        # --- Crumb bar (stays in FilterBar) ---
        self._chips_container = QWidget()
        self._chips_layout = QHBoxLayout(self._chips_container)
        self._chips_layout.setContentsMargins(0, 0, 0, 0)
        self._chips_layout.setSpacing(4)

        self._chips_layout.addStretch()
        self._chips_container.setVisible(False)
        main_layout.addWidget(self._chips_container)

    def get_controls_widget(self) -> QWidget:
        """Return the detachable controls row widget.

        Called by main_window to embed filter controls into the Toolbar.
        The widget is reparented when inserted into the Toolbar's layout.
        """
        return self._controls_widget

    def _setup_filter_menu(self) -> None:
        """Create filter dropdown menu"""
        menu = QMenu(self)

        # Base filter radio group
        self.base_action_group = QActionGroup(self)
        self.base_action_group.setExclusive(True)

        self.action_all_games = menu.addAction(_("All Games"))
        self.action_all_games.setCheckable(True)
        self.action_all_games.setChecked(True)
        self.action_all_games.setData(FILTER_BASE_ALL)
        self.base_action_group.addAction(self.action_all_games)

        self.action_recently_played = menu.addAction(_("Recently Played"))
        self.action_recently_played.setCheckable(True)
        self.action_recently_played.setData(FILTER_BASE_RECENT)
        self.base_action_group.addAction(self.action_recently_played)

        self.action_hidden = menu.addAction(_("Hidden Games"))
        self.action_hidden.setCheckable(True)
        self.action_hidden.setData("hidden")
        self.base_action_group.addAction(self.action_hidden)

        self.base_action_group.triggered.connect(self._on_base_filter_changed)

        menu.addSeparator()

        # Type filter checkboxes
        self.action_favorites = menu.addAction(_("Favorites"))
        self.action_favorites.setCheckable(True)
        self.action_favorites.setData(FILTER_TYPE_FAVORITES)
        self.action_favorites.triggered.connect(self._on_type_filter_toggled)

        self.action_free = menu.addAction(_("Free Games"))
        self.action_free.setCheckable(True)
        self.action_free.setData(FILTER_TYPE_FREE)
        self.action_free.triggered.connect(self._on_type_filter_toggled)

        self.action_demos = menu.addAction(_("Demos"))
        self.action_demos.setCheckable(True)
        self.action_demos.setData(FILTER_TYPE_DEMOS)
        self.action_demos.triggered.connect(self._on_type_filter_toggled)

        self.action_installed = menu.addAction(_("Installed"))
        self.action_installed.setCheckable(True)
        self.action_installed.setData(FILTER_TYPE_INSTALLED)
        self.action_installed.triggered.connect(self._on_type_filter_toggled)

        # Game modes submenu (hidden by default, shown when IGDB is active)
        # Multi-select checkboxes with OR logic
        self.game_modes_menu = QMenu(_("Game Modes"), menu)
        self.game_modes_action = menu.addMenu(self.game_modes_menu)
        self.game_modes_action.setVisible(False)  # Hidden until IGDB is confirmed active

        # Clear action at top of game modes submenu
        clear_modes_action = self.game_modes_menu.addAction(_("Clear Game Modes"))
        clear_modes_action.triggered.connect(self._on_clear_game_modes)
        self.game_modes_menu.addSeparator()

        # Action group for non-exclusive selection
        self.game_mode_action_group = QActionGroup(self)
        self.game_mode_action_group.setExclusive(False)

        for igdb_name, display_name in GAME_MODE_FILTERS.items():
            action = self.game_modes_menu.addAction(_(display_name))
            action.setCheckable(True)
            action.setData(igdb_name)
            action.triggered.connect(self._on_game_mode_toggled)
            self._game_mode_actions[igdb_name] = action
            self.game_mode_action_group.addAction(action)

        # Developers action (opens search+checkbox dialog)
        self.developers_action = menu.addAction(_("Developers..."))
        self.developers_action.setCheckable(True)
        self.developers_action.setVisible(False)
        self.developers_action.triggered.connect(self._on_developers_action)

        # Publishers action (opens search+checkbox dialog)
        self.publishers_action = menu.addAction(_("Publishers..."))
        self.publishers_action.setCheckable(True)
        self.publishers_action.setVisible(False)
        self.publishers_action.triggered.connect(self._on_publishers_action)

        # Genres action (opens search+checkbox dialog)
        self.genres_action = menu.addAction(_("Genres..."))
        self.genres_action.setCheckable(True)
        self.genres_action.setVisible(False)
        self.genres_action.triggered.connect(self._on_genres_action)

        # Tags action (opens search+checkbox dialog with colored indicators)
        self.tags_action = menu.addAction(_("Tags..."))
        self.tags_action.setCheckable(True)
        self.tags_action.setVisible(False)
        self.tags_action.triggered.connect(self._on_tags_action)

        # Release Year action (opens year range dialog)
        self.years_action = menu.addAction(_("Release Year"))
        self.years_action.setCheckable(True)
        self.years_action.setVisible(False)
        self.years_action.triggered.connect(self._on_year_range_action)

        # Separator before compatibility section (hidden by default)
        self.compat_separator = menu.addSeparator()
        self.compat_separator.setVisible(False)

        # Family Shared filter (visible only when family shared games exist)
        self.family_shared_action = menu.addAction(_("Family Shared"))
        self.family_shared_action.setCheckable(True)
        self.family_shared_action.setVisible(False)
        self.family_shared_action.triggered.connect(self._on_family_shared_toggled)

        # Unlinked filter (visible only when orphaned games exist)
        self.orphaned_action = menu.addAction(_("Unlinked"))
        self.orphaned_action.setCheckable(True)
        self.orphaned_action.setVisible(False)
        self.orphaned_action.triggered.connect(self._on_orphaned_toggled)

        # Compatibility filter actions (visible only when data exists)
        self.protondb_action = menu.addAction(_("ProtonDB Rated"))
        self.protondb_action.setCheckable(True)
        self.protondb_action.setVisible(False)
        self.protondb_action.triggered.connect(self._on_protondb_toggled)

        self.steam_deck_action = menu.addAction(_("Deck Verified"))
        self.steam_deck_action.setCheckable(True)
        self.steam_deck_action.setVisible(False)
        self.steam_deck_action.triggered.connect(self._on_steam_deck_toggled)

        # Store separator and placeholder
        self.store_separator = menu.addSeparator()

        # "Exact Stores Only" checkbox (above dynamic store list)
        self._exact_stores_action = menu.addAction(_("Exact Stores Only"))
        self._exact_stores_action.setCheckable(True)
        self._exact_stores_action.setChecked(False)
        self._exact_stores_action.triggered.connect(self._on_exact_stores_toggled)

        self.btn_filter.setMenu(menu)
        self.filter_menu = menu

        # Reset Filters at absolute bottom (stores insert BEFORE this separator)
        self._reset_separator = self.filter_menu.addSeparator()
        self._reset_action = self.filter_menu.addAction(_("Reset Filters"))
        self._reset_action.triggered.connect(self._on_all_clicked)

    def _setup_sort_menu(self) -> None:
        """Create sort dropdown menu"""
        menu = QMenu(self)

        # Toggle options at top
        self.action_reverse = menu.addAction(_("Reverse Sort"))
        self.action_reverse.setCheckable(True)

        self.action_favorites_first = menu.addAction(_("Favorites First"))
        self.action_favorites_first.setCheckable(True)

        menu.addSeparator()

        # Sort mode actions
        self.sort_actions = {}
        sort_modes = [
            (SORT_MODE_RECENT, _("Recently Played")),
            (SORT_MODE_ADDED, _("Added to Catalog")),
            (SORT_MODE_NAME, _("Name")),
            (SORT_MODE_FRANCHISE, _("Franchise")),
            (SORT_MODE_PUBLISHER, _("Publishers")),
            (SORT_MODE_DEVELOPER, _("Developers")),
            (SORT_MODE_RELEASE, _("Release Date")),
        ]

        for mode, label in sort_modes:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setData(mode)
            self.sort_actions[mode] = action

        # Family Licenses sort (hidden by default, shown when data exists)
        family_action = menu.addAction(_("Family Licenses"))
        family_action.setCheckable(True)
        family_action.setData(SORT_MODE_FAMILY_LICENSES)
        family_action.setVisible(False)
        self.sort_actions[SORT_MODE_FAMILY_LICENSES] = family_action

        # Default selection
        self.sort_actions[SORT_MODE_NAME].setChecked(True)

        self.btn_sort.setMenu(menu)
        self.sort_menu = menu

        # Connect signals
        self.sort_menu.triggered.connect(self._on_sort_action)
        self.action_reverse.triggered.connect(self._on_sort_option_changed)
        self.action_favorites_first.triggered.connect(self._on_sort_option_changed)

    def _on_sort_action(self, action: QAction) -> None:
        """Handle sort menu action"""
        mode = action.data()
        if mode is None:
            return

        # Update checked state
        for m, a in self.sort_actions.items():
            a.setChecked(m == mode)

        self._current_sort_mode = mode
        self._update_sort_button_text()
        self._emit_sort_changed()

    def _on_sort_option_changed(self) -> None:
        """Handle reverse/favorites toggle change"""
        self._sort_reverse = self.action_reverse.isChecked()
        self._favorites_first = self.action_favorites_first.isChecked()
        self._emit_sort_changed()

    def _emit_sort_changed(self) -> None:
        """Emit sort changed signal"""
        self.sort_changed.emit(
            self._current_sort_mode,
            self._sort_reverse,
            self._favorites_first
        )

    def _update_sort_button_text(self) -> None:
        """Update sort button text to show current mode"""
        mode_names = {
            SORT_MODE_NAME: _("Name"),
            SORT_MODE_RECENT: _("Recent"),
            SORT_MODE_ADDED: _("Added"),
            SORT_MODE_FRANCHISE: _("Franchise"),
            SORT_MODE_PUBLISHER: _("Publishers"),
            SORT_MODE_DEVELOPER: _("Developers"),
            SORT_MODE_RELEASE: _("Release"),
            SORT_MODE_FAMILY_LICENSES: _("Family Licenses"),
        }
        name = mode_names.get(self._current_sort_mode, _("Sort"))
        self.btn_sort.setText(_("Sort: {mode}").format(mode=name))

    def set_sort_mode(self, mode: str, reverse: bool, favorites_first: bool) -> None:
        """Set current sort mode

        Args:
            mode: Sort mode
            reverse: Reverse sort order
            favorites_first: Show favorites first
        """
        self._current_sort_mode = mode
        self._sort_reverse = reverse
        self._favorites_first = favorites_first

        for m, a in self.sort_actions.items():
            a.setChecked(m == mode)

        self.action_reverse.setChecked(reverse)
        self.action_favorites_first.setChecked(favorites_first)
        self._update_sort_button_text()

    def _on_base_filter_changed(self, action: QAction) -> None:
        """Handle base filter radio change

        When user changes the base filter view (All/Recent/Hidden),
        also clear type filters for a fresh view.
        """
        self._base_filter = action.data()

        # Clear type filters when switching base views (more intuitive UX)
        self._type_filters.clear()
        self.action_favorites.setChecked(False)
        self.action_free.setChecked(False)
        self.action_installed.setChecked(False)

        self._update_button_states()
        self._emit_filters_changed()

    def _on_type_filter_toggled(self) -> None:
        """Handle type filter checkbox toggle"""
        action = self.sender()
        filter_type = action.data()

        if action.isChecked():
            self._type_filters.add(filter_type)
        else:
            self._type_filters.discard(filter_type)

        self._update_button_states()
        self._emit_filters_changed()

    def _on_clear_game_modes(self) -> None:
        """Clear all game mode filters."""
        self._active_game_modes.clear()
        for action in self._game_mode_actions.values():
            action.setChecked(False)
        self._update_button_states()
        self._emit_filters_changed()

    def _on_game_mode_toggled(self) -> None:
        """Handle game mode filter toggle (multi-select with OR logic)"""
        action = self.sender()
        game_mode = action.data()

        if action.isChecked():
            self._active_game_modes.add(game_mode)
        else:
            self._active_game_modes.discard(game_mode)

        self._update_button_states()
        self._emit_filters_changed()

    def set_game_mode_filters(self, modes: list) -> None:
        """Set game mode filters programmatically (e.g. from context menu).

        Args:
            modes: List of IGDB game mode names to activate
        """
        self._active_game_modes = set(modes)
        for name, action in self._game_mode_actions.items():
            action.setChecked(name in self._active_game_modes)
        self._update_button_states()
        self._emit_filters_changed()

    def _on_developers_action(self) -> None:
        """Open developer selection dialog with search."""
        self._show_search_checkbox_dialog(
            _("Filter by Developer"),
            self._available_developers,
            self._active_developers,
            self._apply_developer_selection,
        )
        # Restore correct checkmark after dialog closes (OK or Cancel)
        self._update_button_states()

    def _on_publishers_action(self) -> None:
        """Open publisher selection dialog with search."""
        self._show_search_checkbox_dialog(
            _("Filter by Publisher"),
            self._available_publishers,
            self._active_publishers,
            self._apply_publisher_selection,
        )
        # Restore correct checkmark after dialog closes (OK or Cancel)
        self._update_button_states()

    def _on_genres_action(self) -> None:
        """Open genre selection dialog with search."""
        self._show_search_checkbox_dialog(
            _("Filter by Genre"),
            self._available_genres,
            self._active_genres,
            self._apply_genre_selection,
        )
        self._update_button_states()

    def _on_tags_action(self) -> None:
        """Open tag selection dialog with search and color indicators."""
        available = [name for name, _color, _source in self._available_tag_list]
        self._show_search_checkbox_dialog(
            _("Filter by Tag"),
            available,
            self._active_tags,
            self._apply_tag_selection,
        )
        self._update_button_states()

    def _on_year_range_action(self) -> None:
        """Open year range dialog."""
        current_year = datetime.now().year

        # Pre-fill with current filter range if active, otherwise current year
        if self._active_years:
            year_ints = sorted(int(y) for y in self._active_years if y.isdigit())
            default_start = year_ints[0] if year_ints else current_year
            default_end = year_ints[-1] if year_ints else current_year
        else:
            default_start = current_year
            default_end = current_year

        dialog = QDialog(self)
        dialog.setWindowTitle(_("Filter by Release Year"))
        layout = QVBoxLayout(dialog)

        form = QFormLayout()

        spin_start = QSpinBox()
        spin_start.setRange(1970, current_year + 5)
        spin_start.setValue(default_start)

        spin_end = QSpinBox()
        spin_end.setRange(1970, current_year + 5)
        spin_end.setValue(default_end)

        form.addRow(_("From:"), spin_start)
        form.addRow(_("To:"), spin_end)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        clear_btn = buttons.addButton(_("Clear"), QDialogButtonBox.ButtonRole.ResetRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        cleared = [False]

        def on_clear():
            cleared[0] = True
            dialog.accept()

        clear_btn.clicked.connect(on_clear)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            if cleared[0]:
                self._active_years.clear()
            else:
                start = min(spin_start.value(), spin_end.value())
                end = max(spin_start.value(), spin_end.value())
                self._active_years = {str(y) for y in range(start, end + 1)}
            self._update_button_states()
            self._emit_filters_changed()

    def _on_protondb_toggled(self) -> None:
        """Handle ProtonDB filter toggle."""
        self._filter_protondb = self.protondb_action.isChecked()
        self._update_button_states()
        self._emit_filters_changed()

    def _on_steam_deck_toggled(self) -> None:
        """Handle Steam Deck filter toggle."""
        self._filter_steam_deck = self.steam_deck_action.isChecked()
        self._update_button_states()
        self._emit_filters_changed()

    def _on_family_shared_toggled(self) -> None:
        """Handle Family Shared filter toggle."""
        self._filter_family_shared = self.family_shared_action.isChecked()
        self._update_button_states()
        self._emit_filters_changed()

    def _on_orphaned_toggled(self) -> None:
        """Handle Unlinked (orphaned) filter toggle."""
        self._filter_orphaned = self.orphaned_action.isChecked()
        self._update_button_states()
        self._emit_filters_changed()

    def set_compat_filters_available(
        self, protondb: bool, steam_deck: bool,
        family_shared: bool = False, orphaned: bool = False,
    ) -> None:
        """Show or hide compatibility filter actions based on available data.

        Args:
            protondb: True if any games have ProtonDB ratings
            steam_deck: True if any games have Steam Deck compatibility data
            family_shared: True if any games are family shared
            orphaned: True if any games have no store links
        """
        self.family_shared_action.setVisible(family_shared)
        if not family_shared:
            self._filter_family_shared = False
            self.family_shared_action.setChecked(False)
        self.orphaned_action.setVisible(orphaned)
        if not orphaned:
            self._filter_orphaned = False
            self.orphaned_action.setChecked(False)
        self.compat_separator.setVisible(
            protondb or steam_deck or family_shared or orphaned
        )
        self.protondb_action.setVisible(protondb)
        self.steam_deck_action.setVisible(steam_deck)
        if not protondb:
            self._filter_protondb = False
            self.protondb_action.setChecked(False)
        if not steam_deck:
            self._filter_steam_deck = False
            self.steam_deck_action.setChecked(False)

    def set_family_sort_available(self, visible: bool) -> None:
        """Show or hide the Family Licenses sort option.

        Args:
            visible: True if family sharing data is available
        """
        action = self.sort_actions.get(SORT_MODE_FAMILY_LICENSES)
        if action:
            action.setVisible(visible)

    def _show_search_checkbox_dialog(
        self, title: str, available: List[str], active: Set[str], apply_callback
    ) -> None:
        """Show a dialog with search input, selected panel, and checkable list.

        Uses QListView + QStandardItemModel instead of individual QCheckBox
        widgets to avoid creating thousands of widget objects (virtual scrolling
        means only visible rows create paint operations).

        Args:
            title: Dialog window title
            available: All available items to choose from
            active: Currently active/selected items
            apply_callback: Callback with set of selected items
        """
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        dialog, source_model, cleared = self._build_search_checkbox_dialog(
            title, available, active,
        )
        QApplication.restoreOverrideCursor()

        if dialog.exec() == QDialog.DialogCode.Accepted:
            if cleared[0]:
                apply_callback(set())
            else:
                selected = set()
                for row in range(source_model.rowCount()):
                    item = source_model.item(row)
                    if item.checkState() == Qt.CheckState.Checked:
                        selected.add(item.text())
                apply_callback(selected)

    def _build_search_checkbox_dialog(
        self, title: str, available: List[str], active: Set[str],
    ):
        # Filter out empty/whitespace-only entries
        available = [x for x in available if x and x.strip()]

        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)

        # Search input
        search_input = QLineEdit()
        search_input.setPlaceholderText(_("Search..."))
        layout.addWidget(search_input)

        # --- Selected panel (lightweight: only shows active items) ---
        selected_label = QLabel(_("Selected:"))
        selected_label.setObjectName("hintLabel")
        layout.addWidget(selected_label)

        selected_list = QListView()
        selected_list.setMaximumHeight(120)
        selected_model = QStandardItemModel(selected_list)
        selected_list.setModel(selected_model)
        layout.addWidget(selected_list)

        # --- Full list using QListView with checkable items ---
        cat = title.replace(_("Filter by "), "")
        full_label = QLabel(
            _("All {category}:").format(category=cat)
        )
        full_label.setObjectName("hintLabel")
        layout.addWidget(full_label)

        # Source model with checkable items
        source_model = QStandardItemModel()
        for item_text in available:
            item = QStandardItem(item_text)
            item.setCheckable(True)
            if item_text in active:
                item.setCheckState(Qt.CheckState.Checked)
            item.setEditable(False)
            source_model.appendRow(item)

        # Proxy model for search filtering
        proxy = QSortFilterProxyModel()
        proxy.setSourceModel(source_model)
        proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        full_list = QListView()
        full_list.setModel(proxy)
        full_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        layout.addWidget(full_list)

        def _rebuild_selected_panel():
            """Refresh the selected panel from source model check states."""
            selected_model.clear()
            checked = []
            for row in range(source_model.rowCount()):
                src_item = source_model.item(row)
                if src_item.checkState() == Qt.CheckState.Checked:
                    checked.append(src_item.text())

            for text in sorted(checked):
                sel_item = QStandardItem(text)
                sel_item.setCheckable(True)
                sel_item.setCheckState(Qt.CheckState.Checked)
                sel_item.setEditable(False)
                selected_model.appendRow(sel_item)

            has_selected = selected_model.rowCount() > 0
            selected_label.setVisible(has_selected)
            selected_list.setVisible(has_selected)

        # Build a lookup for selected panel → source model row
        # so unchecking in the selected panel syncs back
        def _item_text_to_source_row() -> Dict[str, int]:
            return {
                source_model.item(r).text(): r
                for r in range(source_model.rowCount())
            }

        def _on_selected_changed(top_left, bottom_right, _roles):
            """Uncheck in selected panel → sync to source model."""
            for row in range(top_left.row(), bottom_right.row() + 1):
                sel_item = selected_model.item(row)
                if sel_item and sel_item.checkState() == Qt.CheckState.Unchecked:
                    lookup = _item_text_to_source_row()
                    src_row = lookup.get(sel_item.text())
                    if src_row is not None:
                        source_model.item(src_row).setCheckState(Qt.CheckState.Unchecked)

        selected_model.dataChanged.connect(_on_selected_changed)

        def _on_source_changed(top_left, bottom_right, _roles):
            """Check/uncheck in full list → rebuild selected panel."""
            _rebuild_selected_panel()

        source_model.dataChanged.connect(_on_source_changed)

        # Initial selected panel state
        _rebuild_selected_panel()

        # Filter by search text
        def on_search_changed(text: str) -> None:
            proxy.setFilterFixedString(text)

        search_input.textChanged.connect(on_search_changed)

        # Buttons: Clear | OK | Cancel
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        clear_btn = buttons.addButton(_("Clear"), QDialogButtonBox.ButtonRole.ResetRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        cleared = [False]

        def on_clear():
            cleared[0] = True
            dialog.accept()

        clear_btn.clicked.connect(on_clear)
        layout.addWidget(buttons)

        dialog.setMinimumWidth(400)
        dialog.setMinimumHeight(500)

        return dialog, source_model, cleared

    def _apply_developer_selection(self, selected: set) -> None:
        """Apply developer filter selection from dialog."""
        self._active_developers = selected
        self._update_button_states()
        self._emit_filters_changed()

    def _apply_publisher_selection(self, selected: set) -> None:
        """Apply publisher filter selection from dialog."""
        self._active_publishers = selected
        self._update_button_states()
        self._emit_filters_changed()

    def _apply_genre_selection(self, selected: set) -> None:
        """Apply genre filter selection from dialog."""
        self._active_genres = selected
        self._update_button_states()
        self._emit_filters_changed()

    def _apply_tag_selection(self, selected: set) -> None:
        """Apply tag filter selection from dialog."""
        self._active_tags = selected
        self._update_button_states()
        self._emit_filters_changed()

    def set_tag_filters(self, tags: list) -> None:
        """Set tag filters programmatically (e.g. from context menu).

        Args:
            tags: List of tag names to activate
        """
        self._active_tags = set(tags)
        self._update_button_states()
        self._emit_filters_changed()

    def set_developer_filters(self, developers: list) -> None:
        """Set developer filters programmatically (e.g. from context menu).

        Args:
            developers: List of developer names to activate
        """
        self._active_developers = set(developers)
        self._update_button_states()
        self._emit_filters_changed()

    def set_publisher_filters(self, publishers: list) -> None:
        """Set publisher filters programmatically (e.g. from context menu).

        Args:
            publishers: List of publisher names to activate
        """
        self._active_publishers = set(publishers)
        self._update_button_states()
        self._emit_filters_changed()

    def set_genre_filters(self, genres: list) -> None:
        """Set genre filters programmatically (e.g. from context menu).

        Args:
            genres: List of genre names to activate
        """
        self._active_genres = set(genres)
        self._update_button_states()
        self._emit_filters_changed()

    def set_year_filters(self, years: list) -> None:
        """Set year filters programmatically (e.g. from context menu).

        Args:
            years: List of year strings to activate
        """
        self._active_years = set(years)
        self._update_button_states()
        self._emit_filters_changed()

    def _on_store_toggled(self, action: QAction) -> None:
        """Handle store filter toggle"""
        store_name = action.data()

        if action.isChecked():
            self._active_stores.add(store_name)
        else:
            # Check if this would be the last store
            if len(self._active_stores) <= 1:
                # Revert the action
                action.setChecked(True)
                QMessageBox.warning(
                    self,
                    _("Filter Error"),
                    _("At least one store must be selected."),
                    QMessageBox.StandardButton.Ok
                )
                return
            self._active_stores.discard(store_name)

        self._update_button_states()
        self._emit_filters_changed()

    def _on_exact_stores_toggled(self, checked: bool) -> None:
        """Handle exact stores filter toggle."""
        self._exact_stores_filter = checked
        self._update_button_states()
        self._emit_filters_changed()

    def _on_all_clicked(self) -> None:
        """Handle All button click - reset all filters to defaults"""
        # Set base filter to All
        self._base_filter = FILTER_BASE_ALL
        self.action_all_games.setChecked(True)

        # Clear type filters (show all types)
        self._type_filters.clear()
        self.action_favorites.setChecked(False)
        self.action_free.setChecked(False)
        self.action_installed.setChecked(False)
        self.action_demos.setChecked(False)

        # Check all stores
        self._active_stores = set(self._available_stores)
        for action in self._store_actions.values():
            action.setChecked(True)

        # Clear tag filters
        self._active_tags.clear()

        # Clear game mode filters
        self._active_game_modes.clear()
        for action in self._game_mode_actions.values():
            action.setChecked(False)

        # Clear developer/publisher/genre/year filters
        self._active_developers.clear()
        self._active_publishers.clear()
        self._active_genres.clear()
        self._active_years.clear()

        # Clear exact stores filter
        self._exact_stores_filter = False
        self._exact_stores_action.setChecked(False)

        # Clear compatibility filters
        self._filter_family_shared = False
        self.family_shared_action.setChecked(False)
        self._filter_orphaned = False
        self.orphaned_action.setChecked(False)
        self._filter_protondb = False
        self.protondb_action.setChecked(False)
        self._filter_steam_deck = False
        self.steam_deck_action.setChecked(False)

        self._update_button_states()
        self._emit_filters_changed()

    def _on_favorites_clicked(self) -> None:
        """Handle Favorites button click - toggle favorites-only filter"""
        is_checked = self.btn_favorites.isChecked()

        if is_checked:
            # Show only favorites
            self._type_filters = {FILTER_TYPE_FAVORITES}
            self.action_favorites.setChecked(True)
            self.action_free.setChecked(False)
            self.action_installed.setChecked(False)
        else:
            # Clear favorites filter
            self._type_filters.discard(FILTER_TYPE_FAVORITES)
            self.action_favorites.setChecked(False)

        self._update_button_states()
        self._emit_filters_changed()

    def _on_tag_filter_toggled(self, tag_name: str, checked: bool) -> None:
        """Handle tag filter toggle"""
        if checked:
            self._active_tags.add(tag_name)
        else:
            self._active_tags.discard(tag_name)

        self._update_button_states()
        self._emit_filters_changed()

    def _on_add_tag_clicked(self) -> None:
        """Handle add tag button click - opens tag manager in settings"""
        self.add_tag_requested.emit()

    def _update_button_states(self) -> None:
        """Update quick button checked states based on filter state"""
        # All button: checked if base is "all", no type/tag/
        # game_mode/dev/pub/genre/year filters, all stores
        is_all = (
            self._base_filter == FILTER_BASE_ALL and
            len(self._type_filters) == 0 and
            len(self._active_game_modes) == 0 and
            len(self._active_developers) == 0 and
            len(self._active_publishers) == 0 and
            len(self._active_genres) == 0 and
            len(self._active_years) == 0 and
            len(self._active_tags) == 0 and
            not self._filter_family_shared and
            not self._filter_orphaned and
            not self._filter_protondb and
            not self._filter_steam_deck and
            not self._exact_stores_filter and
            self._active_stores == set(self._available_stores)
        )
        self.btn_all.setChecked(is_all)

        # Favorites button: checked if ONLY favorites type is selected
        self.btn_favorites.setChecked(
            FILTER_TYPE_FAVORITES in self._type_filters and
            len(self._type_filters) == 1
        )

        # Category checkmarks in filter dropdown
        self.game_modes_action.setChecked(bool(self._active_game_modes))
        self.developers_action.setChecked(bool(self._active_developers))
        self.publishers_action.setChecked(bool(self._active_publishers))
        self.genres_action.setChecked(bool(self._active_genres))
        self.tags_action.setChecked(bool(self._active_tags))
        self.years_action.setChecked(bool(self._active_years))

        # Update tooltips with active filter summary
        self._update_filter_tooltips()

        # Update active filter chips
        self._update_active_chips()

    def _update_filter_tooltips(self) -> None:
        """Update tooltips on filter buttons showing active filter ruleset."""
        lines = [_("<b>Active Filters</b>")]

        # Base filter
        base_labels = {
            "all": _("All Games"),
            "recent": _("Recently Played"),
            "hidden": _("Hidden Games"),
        }
        view = base_labels.get(
            self._base_filter, self._base_filter
        )
        lines.append(_("View: {view}").format(view=view))

        # Type filters
        if self._type_filters:
            type_labels = {
                "favorites": _("Favorites"),
                "free": _("Free Games"),
                "installed": _("Installed"),
                "demos": _("Demos"),
            }
            names = [type_labels.get(t, t) for t in sorted(self._type_filters)]
            lines.append(_("Type: {types}").format(types=", ".join(names)))

        # Stores (only if not all selected)
        if self._active_stores != set(self._available_stores) and self._active_stores:
            store_names = ", ".join(
                sorted(
                    PluginManager.get_store_display_name(s)
                    for s in self._active_stores
                )
            )
            lines.append(
                _("Stores: {stores}").format(stores=store_names)
            )

        # Game modes
        if self._active_game_modes:
            mode_labels = [_(GAME_MODE_FILTERS.get(m, m)) for m in sorted(self._active_game_modes)]
            lines.append(_("Game Modes: {modes}").format(modes=", ".join(mode_labels)))

        # Developers
        if self._active_developers:
            devs = sorted(self._active_developers)
            if len(devs) <= 3:
                lines.append(_("Developers: {devs}").format(devs=", ".join(devs)))
            else:
                lines.append(
                    _("Developers: {devs}... (+{n})").format(
                        devs=", ".join(devs[:3]),
                        n=len(devs) - 3,
                    )
                )

        # Publishers
        if self._active_publishers:
            pubs = sorted(self._active_publishers)
            if len(pubs) <= 3:
                lines.append(_("Publishers: {pubs}").format(pubs=", ".join(pubs)))
            else:
                lines.append(
                    _("Publishers: {pubs}... (+{n})").format(
                        pubs=", ".join(pubs[:3]),
                        n=len(pubs) - 3,
                    )
                )

        # Genres
        if self._active_genres:
            genres = sorted(self._active_genres)
            if len(genres) <= 3:
                lines.append(_("Genres: {genres}").format(genres=", ".join(genres)))
            else:
                lines.append(
                    _("Genres: {genres}... (+{n})").format(
                        genres=", ".join(genres[:3]),
                        n=len(genres) - 3,
                    )
                )

        # Years
        if self._active_years:
            years_sorted = sorted(self._active_years)
            if len(years_sorted) == 1:
                lines.append(_("Year: {year}").format(year=years_sorted[0]))
            else:
                lines.append(
                    _("Years: {start}-{end}").format(
                        start=years_sorted[0],
                        end=years_sorted[-1],
                    )
                )

        # Family Shared
        if self._filter_family_shared:
            lines.append(_("Family Shared"))

        # Unlinked (orphaned)
        if self._filter_orphaned:
            lines.append(_("Unlinked"))

        # Compatibility
        compat_parts = []
        if self._filter_protondb:
            compat_parts.append(_("ProtonDB Rated"))
        if self._filter_steam_deck:
            compat_parts.append(_("Deck Verified"))
        if compat_parts:
            lines.append(_("Compat: {compat}").format(compat=", ".join(compat_parts)))

        # Tags
        if self._active_tags:
            lines.append(_("Tags: {tags}").format(tags=", ".join(sorted(self._active_tags))))

        tooltip = "<br>".join(lines)
        self.btn_filter.setToolTip(tooltip)
        self.btn_all.setToolTip(tooltip)
        self.btn_favorites.setToolTip(tooltip)

    def _update_active_chips(self) -> None:
        """Rebuild the crumb bar: quick-access tags + active filter crumbs."""
        # Clear everything (keep only the stretch at the end)
        while self._chips_layout.count() > 1:
            item = self._chips_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        insert_pos = 0  # Insert before stretch

        # --- Quick-access tags (left side) ---
        has_quick_tags = False
        for tag in self._quick_tags:
            tag_name = tag.get("name", "")
            tag_color = tag.get("color", DEFAULT_TAG_COLOR)
            tag_source = tag.get("source", "native")
            chip = TagChip(tag_name, tag_color, tag_source, self._source_colors_enabled)
            chip.setChecked(tag_name in self._active_tags)
            chip.toggled.connect(
                lambda checked, t=tag_name: self._on_tag_filter_toggled(t, checked)
            )
            self._chips_layout.insertWidget(insert_pos, chip)
            insert_pos += 1
            has_quick_tags = True

        # [+] button after quick tags → opens tag manager
        if has_quick_tags or self._available_tag_list:
            btn_add = QPushButton("+")
            btn_add.setObjectName("addTagButton")
            btn_add.setToolTip(_("Add and manage tags"))
            btn_add.setFixedWidth(28)
            btn_add.clicked.connect(self._on_add_tag_clicked)
            self._chips_layout.insertWidget(insert_pos, btn_add)
            insert_pos += 1

        # Separator between quick tags and filter crumbs
        if has_quick_tags:
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.VLine)
            sep.setFrameShadow(QFrame.Shadow.Sunken)
            sep.setObjectName("toolbarSeparator")
            self._chips_layout.insertWidget(insert_pos, sep)
            insert_pos += 1

        # --- Active filter crumbs (right side) ---
        # chips: list of (label, callback, crumb_type, crumb_color, tooltip_detail)
        chips = []

        # Base filter (only if not default "all")
        if self._base_filter == "recent":
            chips.append((
                _("Recently Played"),
                self._clear_base_filter, "filter", "",
                _("View: Recently Played"),
            ))
        elif self._base_filter == "hidden":
            chips.append((
                _("Hidden Games"),
                self._clear_base_filter, "filter", "",
                _("View: Hidden Games"),
            ))

        # Type filters
        type_labels = {
            "favorites": _("Favorites"),
            "free": _("Free Games"),
            "installed": _("Installed"),
            "demos": _("Demos"),
        }
        for t in sorted(self._type_filters):
            label = type_labels.get(t, t)
            tip = _("Type: {type}").format(type=label)
            chips.append((
                label,
                lambda _t=t: self._remove_type_filter(_t),
                "filter", "", tip,
            ))

        # Exact stores toggle
        if self._exact_stores_filter:
            chips.append((
                _("Exact Stores Only"),
                self._clear_exact_stores_filter,
                "filter", "", "",
            ))

        # Stores (only if not all selected)
        if self._active_stores != set(self._available_stores) and self._active_stores:
            store_names = ", ".join(sorted(
                PluginManager.get_store_display_name(s)
                for s in self._active_stores
            ))
            chips.append((
                _("Stores: {stores}").format(
                    stores=store_names
                ),
                self._clear_store_filter,
                "filter", "", "",
            ))

        # Game modes
        for m in sorted(self._active_game_modes):
            label = _(GAME_MODE_FILTERS.get(m, m))
            tip = _("Game Mode: {mode}").format(mode=label)
            chips.append((
                label,
                lambda _m=m: self._remove_game_mode(_m),
                "filter", "", tip,
            ))

        # Developers
        for d in sorted(self._active_developers):
            tip = _("Developer: {dev}").format(dev=d)
            chips.append((
                d,
                lambda _d=d: self._remove_developer(_d),
                "filter", "", tip,
            ))

        # Publishers
        for p in sorted(self._active_publishers):
            tip = _("Publisher: {pub}").format(pub=p)
            chips.append((
                p,
                lambda _p=p: self._remove_publisher(_p),
                "filter", "", tip,
            ))

        # Genres
        for g in sorted(self._active_genres):
            tip = _("Genre: {genre}").format(genre=g)
            chips.append((
                g,
                lambda _g=g: self._remove_genre(_g),
                "filter", "", tip,
            ))

        # Years
        if self._active_years:
            years_sorted = sorted(self._active_years)
            if len(years_sorted) == 1:
                label = _("Year: {year}").format(year=years_sorted[0])
            else:
                label = _("Years: {start}-{end}").format(
                    start=years_sorted[0],
                    end=years_sorted[-1],
                )
            chips.append((
                label, self._clear_year_filter,
                "filter", "", "",
            ))

        # Family Shared
        if self._filter_family_shared:
            chips.append((_("Family Shared"), self._clear_family_shared_filter, "filter", "", ""))

        # Unlinked (orphaned)
        if self._filter_orphaned:
            chips.append((_("Unlinked"), self._clear_orphaned_filter, "filter", "", ""))

        # Compatibility
        if self._filter_protondb:
            chips.append((
                _("ProtonDB Rated"),
                self._clear_protondb_filter,
                "filter", "",
                _("Compatibility: ProtonDB"),
            ))
        if self._filter_steam_deck:
            chips.append((
                _("Deck Verified"),
                self._clear_steam_deck_filter,
                "filter", "",
                _("Compatibility: Steam Deck"),
            ))

        # Tags
        tag_color_map = {name: color for name, color, _src in self._available_tag_list}
        tag_source_map = {name: src for name, _color, src in self._available_tag_list}
        for t in sorted(self._active_tags):
            color = tag_color_map.get(t, "")
            source = tag_source_map.get(t, "native")
            # Use brand color for bottom border when source colors enabled
            if self._source_colors_enabled and source != "native":
                from ..core.constants import TAG_SOURCE_COLORS
                crumb_color = TAG_SOURCE_COLORS.get(source) or color
            else:
                crumb_color = color
            tip = _("Source: {source}").format(source=source)
            chips.append((
                t,
                lambda _t=t: self._remove_tag_filter(_t),
                "tag", crumb_color, tip,
            ))

        # Create FilterCrumb widgets
        for label, callback, crumb_type, crumb_color, tooltip_detail in chips:
            crumb = FilterCrumb(
                label,
                crumb_type,
                color=crumb_color,
                tooltip_detail=tooltip_detail,
            )
            crumb.clicked.connect(lambda cb=callback: cb())
            self._chips_layout.insertWidget(insert_pos, crumb)
            insert_pos += 1

        has_content = has_quick_tags or bool(self._available_tag_list) or len(chips) > 0
        self._chips_container.setVisible(has_content)
        # Hide the entire filter bar when there are no active crumbs
        # and no quick tags to show
        self.setVisible(has_content)

    # --- Chip removal callbacks ---

    def _clear_base_filter(self) -> None:
        self._base_filter = FILTER_BASE_ALL
        self.action_all_games.setChecked(True)
        self._update_button_states()
        self._emit_filters_changed()

    def _remove_type_filter(self, filter_type: str) -> None:
        self._type_filters.discard(filter_type)
        if filter_type == FILTER_TYPE_FAVORITES:
            self.action_favorites.setChecked(False)
            self.btn_favorites.setChecked(False)
        elif filter_type == FILTER_TYPE_FREE:
            self.action_free.setChecked(False)
        elif filter_type == FILTER_TYPE_INSTALLED:
            self.action_installed.setChecked(False)
        elif filter_type == FILTER_TYPE_DEMOS:
            self.action_demos.setChecked(False)
        self._update_button_states()
        self._emit_filters_changed()

    def _clear_store_filter(self) -> None:
        self._active_stores = set(self._available_stores)
        for action in self._store_actions.values():
            action.setChecked(True)
        self._update_button_states()
        self._emit_filters_changed()

    def _clear_exact_stores_filter(self) -> None:
        self._exact_stores_filter = False
        self._exact_stores_action.setChecked(False)
        self._update_button_states()
        self._emit_filters_changed()

    def _remove_game_mode(self, mode: str) -> None:
        self._active_game_modes.discard(mode)
        if mode in self._game_mode_actions:
            self._game_mode_actions[mode].setChecked(False)
        self._update_button_states()
        self._emit_filters_changed()

    def _remove_developer(self, dev: str) -> None:
        self._active_developers.discard(dev)
        self._update_button_states()
        self._emit_filters_changed()

    def _remove_publisher(self, pub: str) -> None:
        self._active_publishers.discard(pub)
        self._update_button_states()
        self._emit_filters_changed()

    def _remove_genre(self, genre: str) -> None:
        self._active_genres.discard(genre)
        self._update_button_states()
        self._emit_filters_changed()

    def _clear_year_filter(self) -> None:
        self._active_years.clear()
        self._update_button_states()
        self._emit_filters_changed()

    def _clear_protondb_filter(self) -> None:
        self._filter_protondb = False
        self.protondb_action.setChecked(False)
        self._update_button_states()
        self._emit_filters_changed()

    def _clear_steam_deck_filter(self) -> None:
        self._filter_steam_deck = False
        self.steam_deck_action.setChecked(False)
        self._update_button_states()
        self._emit_filters_changed()

    def _clear_family_shared_filter(self) -> None:
        self._filter_family_shared = False
        self.family_shared_action.setChecked(False)
        self._update_button_states()
        self._emit_filters_changed()

    def _clear_orphaned_filter(self) -> None:
        self._filter_orphaned = False
        self.orphaned_action.setChecked(False)
        self._update_button_states()
        self._emit_filters_changed()

    def _remove_tag_filter(self, tag_name: str) -> None:
        self._active_tags.discard(tag_name)
        self._update_button_states()
        self._emit_filters_changed()

    def _emit_filters_changed(self) -> None:
        """Emit filters changed signal"""
        # base_filter is now "all", "recent", or "hidden"
        filters = {
            "base_filter": self._base_filter,
            "type_filters": list(self._type_filters),
            "stores": list(self._active_stores),
            "tags": list(self._active_tags),
            "game_modes": list(self._active_game_modes),
            "developers": list(self._active_developers),
            "publishers": list(self._active_publishers),
            "genres": list(self._active_genres),
            "years": list(self._active_years),
            "filter_family_shared": self._filter_family_shared,
            "filter_orphaned": self._filter_orphaned,
            "filter_protondb": self._filter_protondb,
            "filter_steam_deck": self._filter_steam_deck,
            "exact_stores": self._exact_stores_filter,
        }
        logger.debug(f"Filters changed: {filters}")
        self.filters_changed.emit(filters)

    def set_stores(self, stores: List[tuple[str, str]]) -> None:
        """Set available store filters

        Args:
            stores: List of (store_name, display_name) tuples
        """
        # Remove existing store actions
        for action in self._store_actions.values():
            self.filter_menu.removeAction(action)
        self._store_actions.clear()

        # Track available stores
        self._available_stores = [name for name, _ in stores]

        # Add new store actions before the reset separator
        for store_name, display_name in stores:
            action = QAction(display_name, self.filter_menu)
            action.setCheckable(True)
            action.setChecked(True)  # Default: all stores selected
            action.setData(store_name)
            action.triggered.connect(lambda checked, a=action: self._on_store_toggled(a))
            self.filter_menu.insertAction(self._reset_separator, action)
            self._store_actions[store_name] = action

        # Default: all stores active
        self._active_stores = set(self._available_stores)

    def add_store(self, store_name: str, display_name: str) -> None:
        """Add a single store action incrementally (progressive loading).

        Inserts before the reset separator without clearing existing actions.
        No-op if the store is already present.

        Args:
            store_name: Internal store identifier (e.g. "steam")
            display_name: Human-readable name (e.g. "Steam")
        """
        if store_name in self._store_actions:
            return

        action = QAction(display_name, self.filter_menu)
        action.setCheckable(True)
        action.setChecked(True)
        action.setData(store_name)
        action.triggered.connect(lambda checked, a=action: self._on_store_toggled(a))
        self.filter_menu.insertAction(self._reset_separator, action)

        self._store_actions[store_name] = action
        self._available_stores.append(store_name)
        self._active_stores.add(store_name)

    def set_tags(self, tags: List[tuple[str, str, str]]) -> None:
        """Set available tag filters

        Args:
            tags: List of (tag_name, color, source) tuples
        """
        # Track available tags (visual chips removed — tags are toggled
        # via Filter dropdown / context menu / set_active_filters)
        self._available_tag_list = list(tags)

        # Remove active tags that are no longer available
        available_tag_names = {name for name, _color, _source in tags}
        self._active_tags &= available_tag_names

    def get_active_filters(self) -> dict:
        """Get current filter state"""
        return {
            "base_filter": self._base_filter,
            "type_filters": list(self._type_filters),
            "stores": list(self._active_stores),
            "tags": list(self._active_tags),
            "game_modes": list(self._active_game_modes),
            "developers": list(self._active_developers),
            "publishers": list(self._active_publishers),
            "genres": list(self._active_genres),
            "years": list(self._active_years),
            "filter_family_shared": self._filter_family_shared,
            "filter_orphaned": self._filter_orphaned,
            "filter_protondb": self._filter_protondb,
            "filter_steam_deck": self._filter_steam_deck,
            "exact_stores": self._exact_stores_filter,
        }

    def set_active_filters(self, filters: dict) -> None:
        """Set filter state

        Args:
            filters: Dict with base_filter, type_filters, stores, tags, game_modes
        """
        # Base filter (all, recent, hidden)
        self._base_filter = filters.get("base_filter", FILTER_BASE_ALL)
        self.action_all_games.setChecked(self._base_filter == FILTER_BASE_ALL)
        self.action_recently_played.setChecked(self._base_filter == FILTER_BASE_RECENT)
        self.action_hidden.setChecked(self._base_filter == "hidden")

        # Type filters
        self._type_filters = set(filters.get("type_filters", []))
        self.action_favorites.setChecked(FILTER_TYPE_FAVORITES in self._type_filters)
        self.action_free.setChecked(FILTER_TYPE_FREE in self._type_filters)
        self.action_installed.setChecked(FILTER_TYPE_INSTALLED in self._type_filters)
        self.action_demos.setChecked(FILTER_TYPE_DEMOS in self._type_filters)

        # Stores
        self._active_stores = set(filters.get("stores", self._available_stores))
        for name, action in self._store_actions.items():
            action.setChecked(name in self._active_stores)

        # Tags
        self._active_tags = set(filters.get("tags", []))

        # Game modes (multi-select with OR logic)
        self._active_game_modes = set(filters.get("game_modes", []))
        for name, action in self._game_mode_actions.items():
            action.setChecked(name in self._active_game_modes)

        # Developers / Publishers / Genres / Years (no submenu actions to update)
        self._active_developers = set(filters.get("developers", []))
        self._active_publishers = set(filters.get("publishers", []))
        self._active_genres = set(filters.get("genres", []))
        self._active_years = set(filters.get("years", []))

        # Exact stores filter
        self._exact_stores_filter = filters.get("exact_stores", False)
        self._exact_stores_action.setChecked(self._exact_stores_filter)

        # Only restore data-dependent boolean filters if their data is
        # available (action visible).  Restoring a stale True when the
        # corresponding ID set is empty causes intersection with empty
        # set → 0 games shown.
        if self.family_shared_action.isVisible():
            self._filter_family_shared = filters.get("filter_family_shared", False)
            self.family_shared_action.setChecked(self._filter_family_shared)

        if self.orphaned_action.isVisible():
            self._filter_orphaned = filters.get("filter_orphaned", False)
            self.orphaned_action.setChecked(self._filter_orphaned)

        if self.protondb_action.isVisible():
            self._filter_protondb = filters.get("filter_protondb", False)
            self.protondb_action.setChecked(self._filter_protondb)

        if self.steam_deck_action.isVisible():
            self._filter_steam_deck = filters.get("filter_steam_deck", False)
            self.steam_deck_action.setChecked(self._filter_steam_deck)

        self._update_button_states()

    def set_available_stores(self, stores: List[tuple]) -> None:
        """Set available store filters from game_service format

        Args:
            stores: List of (store_name, display_name, is_authenticated) tuples
        """
        # Convert to the format set_stores expects
        store_list = [(name, display) for name, display, _ in stores]
        self.set_stores(store_list)

    def set_available_tags(self, tags: List[Dict]) -> None:
        """Set available tag filters from game_service format

        Args:
            tags: List of {"name": str, "color": str, "source": str} dicts
        """
        # Convert to the format set_tags expects (3-tuple with source)
        tag_list = [
            (t.get("name", ""), t.get("color", DEFAULT_TAG_COLOR), t.get("source", "native"))
            for t in tags
        ]
        self.set_tags(tag_list)
        # Show/hide Tags... in filter dropdown
        self.tags_action.setVisible(len(tag_list) > 0)

    def set_quick_access_tags(self, tags: List[Dict]) -> None:
        """Set quick-access tags for the crumb bar.

        Quick-access tags appear as toggle buttons on the left side of
        the crumb bar. Scored tags first (by score desc), then by frequency.

        Args:
            tags: List of {"name": str, "color": str, ...} dicts
        """
        self._quick_tags = tags
        self._update_active_chips()

    def set_game_modes_available(self, available: bool) -> None:
        """Show or hide the Game Modes filter submenu

        Args:
            available: True if IGDB is active and game modes data is available
        """
        self.game_modes_action.setVisible(available)
        if not available:
            # Clear any active game mode filters when IGDB becomes unavailable
            self._active_game_modes.clear()
            for action in self._game_mode_actions.values():
                action.setChecked(False)

    def set_available_developers(self, developers: List[str]) -> None:
        """Set available developer filters (for dialog population).

        Args:
            developers: Sorted list of unique developer names
        """
        self._available_developers = [d for d in developers if d and d.strip()]
        self.developers_action.setVisible(len(self._available_developers) > 0)
        # Remove active developers that are no longer available
        self._active_developers &= set(self._available_developers)

    def set_available_publishers(self, publishers: List[str]) -> None:
        """Set available publisher filters (for dialog population).

        Args:
            publishers: Sorted list of unique publisher names
        """
        self._available_publishers = [p for p in publishers if p and p.strip()]
        self.publishers_action.setVisible(len(self._available_publishers) > 0)
        # Remove active publishers that are no longer available
        self._active_publishers &= set(self._available_publishers)

    def set_available_genres(self, genres: List[str]) -> None:
        """Set available genre filters (for dialog population).

        Args:
            genres: Sorted list of unique genre names from IGDB
        """
        self._available_genres = [g for g in genres if g and g.strip()]
        self.genres_action.setVisible(len(self._available_genres) > 0)
        # Remove active genres that are no longer available
        self._active_genres &= set(self._available_genres)

    def set_available_years(self, years: List[str]) -> None:
        """Set available release year filters (for dialog population).

        Args:
            years: List of year strings sorted descending (e.g. ["2025", "2024", ...])
        """
        self._available_years = years
        self.years_action.setVisible(len(years) > 0)
        # Remove active years that are no longer available
        self._active_years &= set(years)

    def _update_dice_icon(self) -> None:
        """Create a theme-aware dice icon for the random button."""
        size = max(18, self.btn_random.height() - 8)
        icon = load_tinted_icon("dice.svg", size=size)
        if icon.isNull():
            self.btn_random.setText("?")
            return
        self.btn_random.setIcon(icon)
        self.btn_random.setIconSize(QSize(size, size))
        self.btn_random.setText("")

    def set_source_colors_enabled(self, enabled: bool) -> None:
        """Enable or disable source brand color accents on tag chips/crumbs.

        Args:
            enabled: True to show brand color accents
        """
        if self._source_colors_enabled != enabled:
            self._source_colors_enabled = enabled
            self._update_active_chips()

    def refresh_icons(self) -> None:
        """Refresh theme-aware icons after a theme change."""
        self._update_dice_icon()
