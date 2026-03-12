# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# config_dialog.py

"""Wine Platform Plugin Configuration Dialog

Unified runtime picker populated from RuntimeScanner. Shows all detected
Wine/Proton/umu installations from Steam, Heroic, Lutris, Bottles, system.
Supports global mode (config.toml) and per-game mode (dict overrides).
Per-runtime settings are keyed by runtime identifier in config.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from luducat.utils.icons import load_tinted_icon

logger = logging.getLogger(__name__)


class WineConfigDialog(QDialog):
    """Configuration dialog for Wine platform plugin.

    Unified runtime picker at top, settings group in middle (settings are
    per-runtime), command preview at bottom.

    Two modes:
    - Global (per_game_config=None): reads/writes config.toml
    - Per-game (per_game_config=dict): reads/writes that dict, returns via get_config()
    """

    connection_status_changed = Signal(str, bool)

    def __init__(
        self,
        config: Any,
        plugin_manager: Any,
        parent: Optional[QDialog] = None,
        *,
        per_game_config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(parent)
        self.config = config
        self.plugin_manager = plugin_manager
        self.plugin_name = "wine"
        self._per_game = per_game_config is not None
        self._pg_config = dict(per_game_config) if per_game_config else {}

        # Scan for installed runtimes
        from .runtime_scanner import scan_installed_runtimes
        self._runtimes = scan_installed_runtimes()
        self._has_gamemode = shutil.which("gamemoderun") is not None

        if self._per_game:
            self.setWindowTitle(_("Wine Options — Per-Game"))
        else:
            self.setWindowTitle(_("Configure Wine"))
        self.setMinimumWidth(672)
        self.setMinimumHeight(462)

        self._setup_ui()
        self._load_settings()
        self._update_settings_for_runtime()
        self._update_preview()
        self.adjustSize()

    def get_config(self) -> Dict[str, Any]:
        """Return per-game config dict (only meaningful in per-game mode)."""
        return dict(self._pg_config)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)

        # === Header ===
        header_layout = QHBoxLayout()
        header_layout.setSpacing(14)

        icon_name = "plug-platform.svg"
        discovered = self.plugin_manager.get_discovered_plugins()
        meta = discovered.get(self.plugin_name)
        if meta and meta.icon and meta.plugin_dir:
            icon_path = meta.plugin_dir / meta.icon
            if icon_path.exists():
                icon_name = str(icon_path)

        icon = load_tinted_icon(icon_name, size=32)
        icon_label = QLabel()
        icon_label.setPixmap(icon.pixmap(32, 32))
        icon_label.setFixedSize(32, 32)
        header_layout.addWidget(icon_label)

        header_text = QVBoxLayout()
        header_text.setSpacing(2)
        version = meta.version if meta else ""
        title = QLabel(f"<b>Wine</b>  v{version}")
        base_size = QApplication.instance().font().pointSize()
        title_font = title.font()
        title_font.setPointSize(base_size + 3)
        title.setFont(title_font)
        header_text.addWidget(title)

        desc = QLabel(_("Run Windows games on Linux using Wine/Proton"))
        desc.setWordWrap(True)
        desc.setObjectName("dialogDescription")
        header_text.addWidget(desc)

        header_layout.addLayout(header_text, 1)
        layout.addLayout(header_layout)

        # === Separator ===
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

        # === Nothing-installed banner ===
        self._nothing_installed_label = QLabel(
            _("No Wine or Proton installation detected. "
              "Install Wine or Proton to launch Windows games.")
        )
        self._nothing_installed_label.setObjectName("hintLabel")
        self._nothing_installed_label.setWordWrap(True)
        self._nothing_installed_label.setVisible(len(self._runtimes) == 0)
        layout.addWidget(self._nothing_installed_label)

        # === Runtime dropdown ===
        runtime_form = QFormLayout()
        runtime_form.setSpacing(8)

        self._runtime_combo = QComboBox()
        if self._per_game:
            self._runtime_combo.addItem(_("Use global default"), "default")

        # Add all detected runtimes (already sorted by recommendation)
        for rt in self._runtimes:
            self._runtime_combo.addItem(rt.display_label, rt.identifier)

        self._runtime_combo.currentIndexChanged.connect(
            self._on_runtime_changed
        )
        runtime_form.addRow(_("Default runtime"), self._runtime_combo)
        layout.addLayout(runtime_form)

        # Per-game: reset button to clear all wine overrides
        if self._per_game:
            self._reset_btn = QPushButton(_("Reset to global default"))
            self._reset_btn.clicked.connect(self._on_reset_to_global)
            layout.addWidget(self._reset_btn)

        # === Settings group ===
        self._settings_group = QGroupBox(_("Settings"))
        settings_layout = QVBoxLayout(self._settings_group)
        settings_layout.setSpacing(8)

        # Checkboxes in 2-column grid
        cb_grid = QGridLayout()
        cb_grid.setSpacing(8)

        self._chk_esync = QCheckBox("ESYNC")
        self._chk_esync.stateChanged.connect(self._update_preview)
        cb_grid.addWidget(self._chk_esync, 0, 0)

        self._chk_fsync = QCheckBox("FSYNC")
        self._chk_fsync.stateChanged.connect(self._update_preview)
        cb_grid.addWidget(self._chk_fsync, 0, 1)

        self._chk_dxvk = QCheckBox("DXVK")
        self._chk_dxvk.stateChanged.connect(self._update_preview)
        cb_grid.addWidget(self._chk_dxvk, 1, 0)

        self._chk_mangohud = QCheckBox("MangoHud")
        self._chk_mangohud.stateChanged.connect(self._update_preview)
        cb_grid.addWidget(self._chk_mangohud, 1, 1)

        self._chk_gamemode = QCheckBox("Gamemode")
        self._chk_gamemode.stateChanged.connect(self._update_preview)
        if not self._has_gamemode:
            self._chk_gamemode.setEnabled(False)
            self._chk_gamemode.setToolTip(
                _("gamemoderun not found in PATH")
            )
        cb_grid.addWidget(self._chk_gamemode, 2, 0)

        self._chk_vd = QCheckBox(_("Virtual Desktop"))
        self._chk_vd.stateChanged.connect(self._on_vd_toggled)
        cb_grid.addWidget(self._chk_vd, 2, 1)

        settings_layout.addLayout(cb_grid)

        # Virtual Desktop resolution
        vd_row = QHBoxLayout()
        self._vd_res_label = QLabel(_("Resolution:"))
        vd_row.addWidget(self._vd_res_label)
        self._vd_res_edit = QLineEdit()
        self._vd_res_edit.setPlaceholderText("1920x1080")
        self._vd_res_edit.setMaximumWidth(140)
        self._vd_res_edit.textChanged.connect(self._update_preview)
        vd_row.addWidget(self._vd_res_edit)
        vd_row.addStretch()
        settings_layout.addLayout(vd_row)

        # WINEDEBUG dropdown
        debug_form = QFormLayout()
        debug_form.setSpacing(8)
        self._winedebug_combo = QComboBox()
        self._winedebug_combo.addItem("fixme-all", "fixme-all")
        self._winedebug_combo.addItem("-all", "-all")
        self._winedebug_combo.addItem("warn+all", "warn+all")
        self._winedebug_combo.addItem(_("(none)"), "")
        self._winedebug_combo.currentIndexChanged.connect(self._update_preview)
        debug_form.addRow("WINEDEBUG", self._winedebug_combo)
        settings_layout.addLayout(debug_form)

        layout.addWidget(self._settings_group)

        # === Command Preview ===
        preview_group = QGroupBox(_("Command Preview"))
        preview_layout = QVBoxLayout(preview_group)
        self._preview = QTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setMaximumHeight(130)
        self._preview.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        preview_layout.addWidget(self._preview)
        layout.addStretch()

        layout.addWidget(preview_group)

        # === Bottom row: [OK] [Cancel] ===
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    # ── VD toggle ─────────────────────────────────────────────────────

    def _on_vd_toggled(self) -> None:
        enabled = self._chk_vd.isChecked()
        self._vd_res_edit.setEnabled(enabled)
        self._vd_res_label.setEnabled(enabled)
        self._update_preview()

    # ── Settings load/save ────────────────────────────────────────────

    def _cfg_key(self, key: str) -> str:
        return f"plugins.{self.plugin_name}.{key}"

    def _get_global(self, key: str, default: Any = None) -> Any:
        """Read a global config value."""
        return self.config.get(self._cfg_key(key), default)

    def _get_runtime_setting(self, runtime_id: str, key: str,
                             default: Any = None) -> Any:
        """Read a per-runtime setting, falling back to global default."""
        if runtime_id:
            rt_key = f"plugins.{self.plugin_name}.runtimes.{runtime_id}.{key}"
            val = self.config.get(rt_key, None)
            if val is not None:
                return val
        return self._get_global(key, default)

    def _load_settings(self) -> None:
        """Load current settings from config (or per-game dict)."""
        if self._per_game:
            self._load_per_game()
        else:
            self._load_global()

    def _load_global(self) -> None:
        """Load settings from config.toml."""
        # Selected runtime — treat legacy "auto" same as empty
        runtime_id = self._get_global("default_runtime", "")
        if runtime_id and runtime_id != "auto":
            idx = self._runtime_combo.findData(runtime_id)
            if idx >= 0:
                self._runtime_combo.setCurrentIndex(idx)
            else:
                # Saved runtime no longer exists, fall back to first
                self._runtime_combo.setCurrentIndex(0)
        else:
            self._runtime_combo.setCurrentIndex(0)

        # Load settings for the currently selected runtime
        self._load_settings_for_runtime(runtime_id)

    def _load_settings_for_runtime(self, runtime_id: str) -> None:
        """Load checkbox/dropdown state for a specific runtime."""
        self._chk_esync.setChecked(
            self._get_runtime_setting(runtime_id, "esync", True)
        )
        self._chk_fsync.setChecked(
            self._get_runtime_setting(runtime_id, "fsync", True)
        )
        self._chk_dxvk.setChecked(
            self._get_runtime_setting(runtime_id, "dxvk", False)
        )
        self._chk_mangohud.setChecked(
            self._get_runtime_setting(runtime_id, "mangohud", False)
        )
        self._chk_gamemode.setChecked(
            self._get_runtime_setting(runtime_id, "gamemode", False)
        )

        vd = self._get_runtime_setting(runtime_id, "virtual_desktop", False)
        self._chk_vd.setChecked(vd)
        vd_res = self._get_runtime_setting(
            runtime_id, "virtual_desktop_resolution", "1920x1080"
        )
        self._vd_res_edit.setText(vd_res)
        self._vd_res_edit.setEnabled(vd)
        self._vd_res_label.setEnabled(vd)

        debug_val = self._get_runtime_setting(
            runtime_id, "winedebug", "fixme-all"
        )
        debug_idx = self._winedebug_combo.findData(debug_val)
        if debug_idx >= 0:
            self._winedebug_combo.setCurrentIndex(debug_idx)

    def _load_per_game(self) -> None:
        """Load settings from per-game config dict, falling back to global."""
        pg = self._pg_config

        # Runtime selection
        runtime_id = pg.get("wine_runtime", "")
        if runtime_id:
            idx = self._runtime_combo.findData(runtime_id)
            if idx >= 0:
                self._runtime_combo.setCurrentIndex(idx)
            else:
                self._runtime_combo.setCurrentIndex(0)  # "Use global default"
        else:
            self._runtime_combo.setCurrentIndex(0)  # "Use global default"

        # Settings — per-game value if set, else global
        self._chk_esync.setChecked(
            pg.get("wine_esync", self._get_global("esync", True))
        )
        self._chk_fsync.setChecked(
            pg.get("wine_fsync", self._get_global("fsync", True))
        )
        self._chk_dxvk.setChecked(
            pg.get("wine_dxvk", self._get_global("dxvk", False))
        )
        self._chk_mangohud.setChecked(
            pg.get("wine_mangohud", self._get_global("mangohud", False))
        )
        self._chk_gamemode.setChecked(
            pg.get("wine_gamemode", self._get_global("gamemode", False))
        )

        vd = pg.get(
            "wine_virtual_desktop",
            self._get_global("virtual_desktop", False),
        )
        self._chk_vd.setChecked(vd)
        vd_res = pg.get(
            "wine_virtual_desktop_resolution",
            self._get_global("virtual_desktop_resolution", "1920x1080"),
        )
        self._vd_res_edit.setText(vd_res)
        self._vd_res_edit.setEnabled(vd)
        self._vd_res_label.setEnabled(vd)

        debug_val = pg.get(
            "wine_winedebug",
            self._get_global("winedebug", "fixme-all"),
        )
        debug_idx = self._winedebug_combo.findData(debug_val)
        if debug_idx >= 0:
            self._winedebug_combo.setCurrentIndex(debug_idx)

    def _save_settings(self) -> None:
        """Save settings to config.toml or per-game dict."""
        if self._per_game:
            self._save_per_game()
        else:
            self._save_global()

    def _save_global(self) -> None:
        """Save settings to config.toml."""
        runtime_data = self._runtime_combo.currentData()

        # Save selected runtime
        self.config.set(
            self._cfg_key("default_runtime"), runtime_data or ""
        )
        # Derive runtime_mode from selected runtime type
        rt = self._find_runtime(runtime_data) if runtime_data else None
        if rt:
            self.config.set(self._cfg_key("runtime_mode"), rt.runtime_type)
        else:
            self.config.set(self._cfg_key("runtime_mode"), "auto")

        # Save global defaults
        self.config.set(self._cfg_key("esync"), self._chk_esync.isChecked())
        self.config.set(self._cfg_key("fsync"), self._chk_fsync.isChecked())
        self.config.set(self._cfg_key("dxvk"), self._chk_dxvk.isChecked())
        self.config.set(self._cfg_key("mangohud"), self._chk_mangohud.isChecked())
        self.config.set(self._cfg_key("gamemode"), self._chk_gamemode.isChecked())
        self.config.set(
            self._cfg_key("virtual_desktop"),
            self._chk_vd.isChecked(),
        )
        vd_res = self._vd_res_edit.text().strip() or "1920x1080"
        self.config.set(
            self._cfg_key("virtual_desktop_resolution"),
            vd_res,
        )
        self.config.set(
            self._cfg_key("winedebug"),
            self._winedebug_combo.currentData(),
        )

        # Also save as per-runtime overrides if a specific runtime is selected
        if runtime_data:
            rt_prefix = f"plugins.{self.plugin_name}.runtimes.{runtime_data}"
            self.config.set(f"{rt_prefix}.esync", self._chk_esync.isChecked())
            self.config.set(f"{rt_prefix}.fsync", self._chk_fsync.isChecked())
            self.config.set(f"{rt_prefix}.dxvk", self._chk_dxvk.isChecked())
            self.config.set(
                f"{rt_prefix}.mangohud", self._chk_mangohud.isChecked()
            )
            self.config.set(
                f"{rt_prefix}.gamemode", self._chk_gamemode.isChecked()
            )
            self.config.set(
                f"{rt_prefix}.virtual_desktop", self._chk_vd.isChecked()
            )
            self.config.set(
                f"{rt_prefix}.virtual_desktop_resolution", vd_res
            )
            self.config.set(
                f"{rt_prefix}.winedebug",
                self._winedebug_combo.currentData(),
            )

    def _save_per_game(self) -> None:
        """Save per-game overrides — only non-default values stored."""
        pg = {}

        runtime_data = self._runtime_combo.currentData()
        if runtime_data != "default":
            pg["wine_runtime"] = runtime_data

        # Checkboxes — store if differs from global
        pairs = [
            ("wine_esync", self._chk_esync.isChecked(),
             self._get_global("esync", True)),
            ("wine_fsync", self._chk_fsync.isChecked(),
             self._get_global("fsync", True)),
            ("wine_dxvk", self._chk_dxvk.isChecked(),
             self._get_global("dxvk", False)),
            ("wine_mangohud", self._chk_mangohud.isChecked(),
             self._get_global("mangohud", False)),
            ("wine_gamemode", self._chk_gamemode.isChecked(),
             self._get_global("gamemode", False)),
            ("wine_virtual_desktop", self._chk_vd.isChecked(),
             self._get_global("virtual_desktop", False)),
        ]

        vd_res = self._vd_res_edit.text().strip() or "1920x1080"
        global_vd_res = self._get_global(
            "virtual_desktop_resolution", "1920x1080"
        )
        if vd_res != global_vd_res:
            pg["wine_virtual_desktop_resolution"] = vd_res

        for key, val, global_val in pairs:
            if val != global_val:
                pg[key] = val

        debug = self._winedebug_combo.currentData()
        if debug != self._get_global("winedebug", "fixme-all"):
            pg["wine_winedebug"] = debug

        self._pg_config = pg

    # ── Runtime switching ─────────────────────────────────────────────

    def _on_runtime_changed(self) -> None:
        """When runtime selection changes, reload settings for that runtime."""
        self._update_settings_for_runtime()
        self._update_preview()

    def _update_settings_for_runtime(self) -> None:
        """Load saved settings for the currently selected runtime."""
        if self._per_game:
            return  # Per-game mode doesn't switch settings per runtime

        runtime_data = self._runtime_combo.currentData()
        self._load_settings_for_runtime(runtime_data or "")

    def _find_runtime(self, identifier: str):
        """Find an InstalledRuntime by identifier from cached list."""
        for rt in self._runtimes:
            if rt.identifier == identifier:
                return rt
        return None

    # ── Command Preview ───────────────────────────────────────────────

    def _update_preview(self) -> None:
        """Build and display the command preview."""
        runtime_data = self._runtime_combo.currentData()
        if runtime_data == "default":
            rt = None
        else:
            rt = self._find_runtime(runtime_data)

        env_lines = []
        cmd_parts = []

        if rt and rt.runtime_type == "proton":
            env_lines.append(f"PROTONPATH={rt.path}")
            env_lines.append("STEAM_COMPAT_DATA_PATH=/path/to/prefix")

            if self._chk_gamemode.isChecked():
                cmd_parts.append("gamemoderun")
            if self._chk_mangohud.isChecked():
                cmd_parts.append("mangohud")

            proton_script = str(rt.path / "proton")
            cmd_parts.extend([proton_script, "waitforexitandrun", "game.exe"])

        elif rt and rt.runtime_type == "umu":
            env_lines.append("GAMEID=umu-12345")
            env_lines.append("STORE=gog")
            # umu-run uses Proton — show PROTONPATH placeholder
            env_lines.append("PROTONPATH=/path/to/Proton")

            if self._chk_gamemode.isChecked():
                cmd_parts.append("gamemoderun")
            if self._chk_mangohud.isChecked():
                cmd_parts.append("mangohud")

            cmd_parts.extend([str(rt.wine_binary), "game.exe"])

        else:
            # Default or Wine runtime
            wine_bin = str(rt.wine_binary) if rt else "/usr/bin/wine"
            env_lines.append("WINEPREFIX=/path/to/prefix")
            env_lines.append("WINEARCH=win64")

            if self._chk_esync.isChecked():
                env_lines.append("WINEESYNC=1")
            if self._chk_fsync.isChecked():
                env_lines.append("WINEFSYNC=1")

            debug = self._winedebug_combo.currentData()
            if debug:
                env_lines.append(f"WINEDEBUG={debug}")

            if self._chk_dxvk.isChecked():
                env_lines.append('WINEDLLOVERRIDES="d3d11,dxgi=n"')

            if self._chk_gamemode.isChecked():
                cmd_parts.append("gamemoderun")
            if self._chk_mangohud.isChecked():
                cmd_parts.append("mangohud")

            if self._chk_vd.isChecked():
                res = self._vd_res_edit.text().strip() or "1920x1080"
                cmd_parts.append(wine_bin)
                cmd_parts.append("explorer")
                cmd_parts.append(f"/desktop=luducat,{res}")
                cmd_parts.append("game.exe")
            else:
                cmd_parts.append(wine_bin)
                cmd_parts.append("game.exe")

        text = " ".join(env_lines)
        if text:
            text += "\n"
        text += " ".join(cmd_parts)
        self._preview.setPlainText(text)

    # ── Per-game reset ─────────────────────────────────────────────────

    def _on_reset_to_global(self) -> None:
        """Reset all per-game wine settings to global defaults."""
        reply = QMessageBox.question(
            self,
            _("Reset Wine Settings"),
            _("Clear all per-game Wine overrides and use global defaults?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._pg_config = {}
        self._runtime_combo.setCurrentIndex(0)
        self._load_per_game()

    # ── Accept / Cancel ───────────────────────────────────────────────

    def _on_accept(self) -> None:
        self._save_settings()
        self.accept()
