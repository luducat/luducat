# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# metadata_preview.py

"""Metadata priority preview dialog for luducat

Shows a live preview of how current metadata priority settings would resolve
for a selected game. Helps users understand the effect of their configurations.
"""

import logging
from typing import Any, Dict, List, Optional, Set

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from luducat.core.metadata_resolver import (
    FIELD_GROUPS,
    FIELD_LABELS,
    SOURCE_LABELS,
    MetadataResolver,  # for _PRIORITY_FIELD_ALIASES
)
from luducat.core.plugin_manager import PluginManager

logger = logging.getLogger(__name__)



class MetadataPreviewDialog(QDialog):
    """Dialog for previewing metadata priority resolution

    Shows a two-panel view:
    - Left: Game list with search and favorites filter
    - Right: Preview of how metadata fields resolve with source indicators

    This dialog reads the CURRENT (possibly unsaved) priorities to show
    how they would affect a selected game's metadata.
    """

    def __init__(
        self,
        game_service,
        priorities: Dict[str, List[str]],
        enabled_plugins: Set[str],
        parent: Optional[QWidget] = None,
    ):
        """Initialize the preview dialog

        Args:
            game_service: GameService instance for loading game data
            priorities: Current priority configuration (possibly unsaved)
            enabled_plugins: Set of enabled plugin names
            parent: Parent widget
        """
        super().__init__(parent)
        self._game_service = game_service
        self._priorities = priorities
        self._enabled_plugins = enabled_plugins
        self._all_games: List[Dict[str, Any]] = []
        self._current_game_id: Optional[str] = None

        # Cache for plugin authentication status (checked once per preview session)
        self._plugin_auth_cache: Dict[str, bool] = {}

        # Store UNSAVED priorities locally for preview — does NOT create a
        # second MetadataResolver.  The global singleton is NOT modified.
        self._preview_priorities: Dict[str, List[str]] = dict(priorities) if priorities else {}

        self.setWindowTitle(_("Metadata Priority Preview"))
        self.setMinimumSize(900, 600)
        self.resize(1000, 700)

        self._setup_ui()
        self._load_games()

    def _setup_ui(self) -> None:
        """Create dialog UI"""
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Warning banner
        warning = QLabel(_("This is only a preview, not the full game view."))
        warning.setWordWrap(True)
        from luducat.utils.style_helpers import set_status_property
        set_status_property(warning, "error", bold=True)
        layout.addWidget(warning)

        # Header description
        desc = QLabel(
            _("This preview shows how your current priority settings resolve metadata.") + "\n"
            + _("Select a game to see which source provides each field.")
        )
        desc.setWordWrap(True)
        desc.setObjectName("dialogDescription")
        layout.addWidget(desc)

        # Main splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # Left panel: Game list
        left_panel = self._create_game_list_panel()
        splitter.addWidget(left_panel)

        # Right panel: Preview
        right_panel = self._create_preview_panel()
        splitter.addWidget(right_panel)

        # Set splitter sizes (roughly 1:2 ratio)
        splitter.setSizes([300, 600])

        layout.addWidget(splitter, 1)

        # Dialog buttons
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _create_game_list_panel(self) -> QWidget:
        """Create the left panel with game list"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # Search box
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(_("Search games..."))
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(self._filter_games)
        layout.addWidget(self._search_input)

        # Game list
        self._game_list = QListWidget()
        self._game_list.setAlternatingRowColors(True)
        self._game_list.currentItemChanged.connect(self._on_game_selected)
        layout.addWidget(self._game_list, 1)

        # Filter checkbox
        self._favorites_only = QCheckBox(_("Favorites only"))
        self._favorites_only.stateChanged.connect(self._filter_games)
        layout.addWidget(self._favorites_only)

        return panel

    def _create_preview_panel(self) -> QWidget:
        """Create the right panel with metadata preview"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # Scroll area for preview content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        # Preview content widget
        self._preview_content = QWidget()
        self._preview_layout = QVBoxLayout(self._preview_content)
        self._preview_layout.setSpacing(16)

        # Game header (title + basic info)
        self._header_label = QLabel()
        self._header_label.setWordWrap(True)
        self._header_label.setObjectName("previewHeader")
        self._preview_layout.addWidget(self._header_label)

        # Create field groups
        self._field_labels: Dict[str, QLabel] = {}
        for group_name, fields in FIELD_GROUPS.items():
            group = self._create_field_group(group_name, fields)
            self._preview_layout.addWidget(group)

        self._preview_layout.addStretch()

        scroll.setWidget(self._preview_content)
        layout.addWidget(scroll, 1)

        # Placeholder when no game selected
        self._placeholder = QLabel(_("Select a game to preview metadata"))
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setObjectName("hintLabel")
        layout.addWidget(self._placeholder)

        # Initially show placeholder
        self._preview_content.hide()

        return panel

    def _create_field_group(self, group_name: str, fields: List[str]) -> QGroupBox:
        """Create a group box for a category of fields"""
        group = QGroupBox(group_name)
        layout = QFormLayout(group)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(8)

        for field in fields:
            label = _(FIELD_LABELS.get(field, field.title()))

            # Value label with source indicator
            value_label = QLabel("—")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self._field_labels[field] = value_label

            layout.addRow(f"{label}:", value_label)

        return group

    def _load_games(self) -> None:
        """Load all games from the game service"""
        try:
            self._all_games = self._game_service.get_all_games()
            self._filter_games()
        except Exception as e:
            logger.error(f"Failed to load games for preview: {e}")

    @Slot()
    def _filter_games(self) -> None:
        """Filter and update the game list"""
        self._game_list.clear()

        search_text = self._search_input.text().lower()
        favorites_only = self._favorites_only.isChecked()

        # Sort: favorites first, then alphabetically
        games = sorted(
            self._all_games,
            key=lambda g: (not g.get("is_favorite", False), g.get("title", "").lower())
        )

        for game in games:
            title = game.get("title", _("Unknown"))

            # Apply filters
            if search_text and search_text not in title.lower():
                continue
            if favorites_only and not game.get("is_favorite", False):
                continue

            # Create list item
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, game.get("id"))

            # Mark favorites with star
            if game.get("is_favorite", False):
                item.setText(f"\u2605 {title}")
            else:
                item.setText(title)

            self._game_list.addItem(item)

    @Slot(QListWidgetItem, QListWidgetItem)
    def _on_game_selected(
        self, current: Optional[QListWidgetItem], _previous: Optional[QListWidgetItem]
    ) -> None:
        """Handle game selection change"""
        if not current:
            self._clear_preview()
            return

        game_id = current.data(Qt.ItemDataRole.UserRole)
        if game_id:
            self._load_preview(game_id)

    def _clear_preview(self) -> None:
        """Clear the preview panel"""
        self._preview_content.hide()
        self._placeholder.show()
        self._current_game_id = None

    def _load_preview(self, game_id: str) -> None:
        """Load and display preview for a game"""
        self._current_game_id = game_id
        self._placeholder.hide()
        self._preview_content.show()

        # Find game in cache
        game = None
        for g in self._all_games:
            if g.get("id") == game_id:
                game = g
                break

        if not game:
            self._header_label.setText(_("Game not found"))
            return

        # Update header
        title = game.get("title", _("Unknown"))
        stores = game.get("stores", [])
        store_names = ", ".join(stores) if stores else _("No stores")
        self._header_label.setText(f"<h2>{title}</h2><p>{_('Available in:')} {store_names}</p>")

        # Show busy cursor while querying sources
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            # Load metadata from database
            self._update_field_values(game_id, game)
        finally:
            QApplication.restoreOverrideCursor()

    def _update_field_values(self, game_id: str, game: Dict[str, Any]) -> None:
        """Update all field values by querying sources in priority order

        For each field, iterates through sources according to the field's
        priority list (from unsaved preview settings). Queries each source
        LIVE until data is found.
        """
        # Get detailed game data with store info
        game_data = self._game_service.get_game_details(game_id)
        if not game_data:
            for field in self._field_labels:
                self._field_labels[field].setText("—")
            return

        # Build store_app_ids map and cache store metadata
        store_app_ids: Dict[str, str] = {}
        store_metadata_cache: Dict[str, Dict[str, Any]] = {}
        store_games = game_data.get("store_games", [])
        for sg in store_games:
            store_name = sg.get("store_name", "unknown")
            store_app_ids[store_name] = sg.get("store_app_id", "")
            store_metadata_cache[store_name] = sg.get("metadata", {}) or {}

        game_title = game_data.get("title", "")

        # Get plugin manager for live queries
        plugin_manager = getattr(self._game_service, "plugin_manager", None)

        # Field mappings: field_name -> (metadata_key, extra_key_or_None)
        field_mappings = {
            "title": "title",
            "description": "description",
            "developers": "developers",
            "publishers": "publishers",
            "genres": "genres",
            "release_date": "release_date",
            "cover": "cover_url",
            "screenshots": "screenshots",
            "hero": "background_url",
            "franchise": "franchise",
            "series": "series",
            "themes": "themes",
            "engine": "engine",
            "perspectives": "perspectives",
            "platforms": "platforms",
            "age_rating_esrb": "age_ratings",
            "age_rating_pegi": "age_ratings",
            "links": "websites",
            "rating": "user_rating",
            "critic_rating": "critic_rating",
            "game_modes_detail": "game_modes_detail",
            "crossplay": "crossplay",
            "controller_support": "controller_support",
        }

        # Cache for plugin metadata (so we don't re-query same plugin multiple times)
        plugin_metadata_cache: Dict[str, Dict[str, Any]] = {}

        for field, label_widget in self._field_labels.items():
            value = None
            source = None

            mapping = field_mappings.get(field)
            if not mapping:
                label_widget.setText("—")
                continue

            metadata_key = mapping

            # Get priority list for THIS field from the UNSAVED preview settings
            # This is the user's current (possibly modified) priority order
            canonical = MetadataResolver._PRIORITY_FIELD_ALIASES.get(field, field)
            priority = self._preview_priorities.get(
                canonical, self._preview_priorities.get(field, [])
            )

            logger.debug(f"Preview field '{field}': priority sequence = {priority}")

            # Query sources STRICTLY in priority order until we find data
            # The first source (index 0) has highest priority
            for priority_index, src in enumerate(priority):
                logger.debug(
                    f"Preview field '{field}': trying source #{priority_index}: '{src}'"
                )

                src_value = self._query_source_for_field(
                    src,
                    metadata_key,
                    store_app_ids,
                    store_metadata_cache,
                    plugin_metadata_cache,
                    plugin_manager,
                    game_title,
                )

                if self._is_non_empty(src_value):
                    value = src_value
                    source = src
                    logger.debug(
                        f"Preview field '{field}': FOUND data "
                        f"from '{src}' (priority #{priority_index})"
                    )
                    break  # Found data from highest priority source that has it
                else:
                    logger.debug(
                        f"Preview field '{field}': no data from '{src}', trying next..."
                    )

            # Format and display
            display = self._format_value(field, value)

            if source and value is not None:
                source_label = SOURCE_LABELS.get(
                    source,
                    PluginManager.get_store_display_name(source),
                )
                display = (
                    f"<span style='color: palette(placeholder-text);'>"
                    f"({_('from')} {source_label})</span> {display}"
                )

            label_widget.setText(display)

    def _query_source_for_field(
        self,
        source_name: str,
        metadata_key: str,
        store_app_ids: Dict[str, str],
        store_metadata_cache: Dict[str, Dict[str, Any]],
        plugin_metadata_cache: Dict[str, Dict[str, Any]],
        plugin_manager: Any,
        game_title: str,
    ) -> Any:
        """Query a specific source for a field value

        For store sources (steam, gog, epic): use cached store metadata
        For metadata plugins (igdb, steamgriddb, pcgamingwiki): query live
        """
        # Check if it's a store source with cached data
        if source_name in store_metadata_cache:
            store_meta = store_metadata_cache[source_name]
            result = store_meta.get(metadata_key)
            if result is not None:
                return result
            # Cache miss for this field - fall through to try live plugin query

        # It's a metadata plugin - query live
        if not plugin_manager:
            return None

        # Check plugin metadata cache first (already queried this plugin)
        if source_name in plugin_metadata_cache:
            plugin_meta = plugin_metadata_cache[source_name]
            return plugin_meta.get(metadata_key)

        # Try to get the plugin - check BOTH store plugins AND metadata plugins
        # get_plugin() only returns store plugins, so also check get_metadata_plugins()
        plugin = plugin_manager.get_plugin(source_name)
        if not plugin:
            # Try metadata plugins
            metadata_plugins = plugin_manager.get_metadata_plugins()
            plugin = metadata_plugins.get(source_name)

        if not plugin:
            logger.debug(f"Preview: Plugin '{source_name}' not found")
            return None

        if not plugin.is_available():
            logger.debug(f"Preview: Plugin '{source_name}' not available")
            return None

        # Check authentication with caching (only check once per preview session)
        if source_name not in self._plugin_auth_cache:
            if hasattr(plugin, 'is_authenticated'):
                is_auth = plugin.is_authenticated()
                self._plugin_auth_cache[source_name] = is_auth
                logger.debug(f"Preview: Plugin '{source_name}' auth check: {is_auth}")
            else:
                # Plugin doesn't require auth
                self._plugin_auth_cache[source_name] = True

        if not self._plugin_auth_cache[source_name]:
            logger.debug(f"Preview: Plugin '{source_name}' not authenticated (cached)")
            return None

        # Query plugin for metadata
        metadata = None

        # Method 1: get_metadata_for_store_game (metadata plugins like IGDB, SteamGridDB)
        if hasattr(plugin, "get_metadata_for_store_game"):
            for store_name, app_id in store_app_ids.items():
                if not app_id:
                    continue
                try:
                    metadata = plugin.get_metadata_for_store_game(
                        store_name, app_id, normalized_title=game_title
                    )
                    if metadata:
                        logger.debug(
                            f"Preview: Got metadata from "
                            f"{source_name} via "
                            f"get_metadata_for_store_game for "
                            f"{store_name}/{app_id}: "
                            f"{list(metadata.keys())}"
                        )
                        break
                except Exception as e:
                    logger.warning(
                        f"Preview: Failed to query {source_name}"
                        f" via get_metadata_for_store_game: {e}"
                    )

        # Method 2: get_game_metadata (store plugins like steam, gog, epic)
        if not metadata and hasattr(plugin, "get_game_metadata"):
            app_id = store_app_ids.get(source_name)
            if app_id:
                try:
                    metadata = plugin.get_game_metadata(app_id)
                    if metadata:
                        logger.debug(
                            f"Preview: Got metadata from {source_name} via get_game_metadata "
                            f"for {app_id}: {list(metadata.keys())}"
                        )
                except Exception as e:
                    logger.warning(
                        f"Preview: Failed to query "
                        f"{source_name} via "
                        f"get_game_metadata: {e}"
                    )

        if not metadata:
            return None

        # Cache the result for this plugin
        plugin_metadata_cache[source_name] = metadata

        # Return the requested field (simple top-level lookup)
        return metadata.get(metadata_key)

    @staticmethod
    def _is_non_empty(value: Any) -> bool:
        """Check if a value is non-empty"""
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        if isinstance(value, (list, dict)) and len(value) == 0:
            return False
        return True

    def _format_value(self, field: str, value: Any) -> str:
        """Format a field value for display"""
        if value is None:
            return "—"

        # Special handling for ESRB/PEGI age ratings
        # age_ratings is a list of {"system": "ESRB", "rating": "M"} dicts
        if field == "age_rating_esrb" and isinstance(value, list):
            esrb = [r.get("rating", "") for r in value if r.get("system") == "ESRB"]
            return esrb[0] if esrb else "—"
        if field == "age_rating_pegi" and isinstance(value, list):
            pegi = [r.get("rating", "") for r in value if r.get("system") == "PEGI"]
            return pegi[0] if pegi else "—"

        # Handle lists - show ALL items (no truncation)
        if isinstance(value, list):
            if not value:
                return "—"
            return ", ".join(str(v) for v in value)

        # Handle dicts (like multiplayer)
        if isinstance(value, dict):
            if not value:
                return "—"
            parts = []
            for k, v in value.items():
                if v is True:
                    parts.append(k.replace("_", " ").title())
                elif v:
                    parts.append(f"{k}: {v}")
            return ", ".join(parts) if parts else "—"

        # Handle booleans
        if isinstance(value, bool):
            return _("Yes") if value else _("No")

        # Handle URLs (truncate)
        if isinstance(value, str) and value.startswith("http"):
            if len(value) > 60:
                return f"{value[:57]}..."
            return value

        return str(value)
