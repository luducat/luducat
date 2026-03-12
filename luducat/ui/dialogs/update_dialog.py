# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# update_dialog.py

"""Update notification dialog for luducat

Reusable dialog shown when a new version is available. Used from
startup auto-check, Settings → Check Now, and About → Update link.
"""

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

from ...core.constants import APP_NAME, APP_VERSION

if TYPE_CHECKING:
    from ...core.config import Config


class UpdateDialog(QDialog):
    """Full update notification dialog with changelog and backup options."""

    def __init__(
        self,
        new_version: str,
        config: "Config",
        changelog: Optional[Dict[str, List[str]]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._config = config
        self._new_version = new_version
        self._changelog = changelog
        self._action = "dismiss"
        self._backup_path: Optional[Path] = None
        self._path_changed = False
        self._chk_backup: Optional[QCheckBox] = None
        self._chk_schedule: Optional[QCheckBox] = None

        self.setObjectName("updateDialog")
        self.setWindowTitle(_("Update Available"))
        self.setMinimumWidth(500)
        self.setMinimumHeight(500)
        self._setup_ui()

    def _setup_ui(self) -> None:
        from ...core.backup_manager import get_backup_dir, get_backup_status

        outer_layout = QVBoxLayout(self)
        outer_layout.setSpacing(8)

        # Scrollable content area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(8)

        # Title
        title = QLabel(_("{app_name} {version} is available!").format(
            app_name=APP_NAME, version=self._new_version
        ))
        title.setObjectName("aboutTitle")
        layout.addWidget(title)

        # Subtitle
        subtitle = QLabel(_("You are running version {version}.").format(
            version=APP_VERSION
        ))
        layout.addWidget(subtitle)

        layout.addSpacing(4)

        # --- Changelog section ---
        if self._changelog:
            from ...core.news import format_summary_html
            html = format_summary_html(self._changelog)
            if html:
                changelog_view = QTextBrowser()
                changelog_view.setObjectName("updateChangelog")
                changelog_view.setHtml(html)
                changelog_view.setOpenExternalLinks(False)
                changelog_view.setMaximumHeight(300)
                layout.addWidget(changelog_view)
                layout.addSpacing(4)

        # --- Backup section ---
        schedule_enabled = self._config.get("backup.schedule_enabled", False)
        status = get_backup_status(self._config)
        last_backup = status["last_backup"]  # datetime or None

        backup_stale = True
        if last_backup:
            age = datetime.now() - last_backup
            backup_stale = age.total_seconds() >= 86400  # 24 hours

        self._backup_path = get_backup_dir(self._config)

        if backup_stale:
            if schedule_enabled:
                # Backups enabled but stale — offer a snapshot
                self._chk_backup = QCheckBox(_("Create a backup before updating"))
            else:
                # Backups not enabled — offer to enable + snapshot
                self._chk_backup = QCheckBox(
                    _("Enable backups and create a snapshot")
                )
            self._chk_backup.setChecked(True)
            layout.addWidget(self._chk_backup)

            # Path row (indented under checkbox)
            path_row = QHBoxLayout()
            path_row.setContentsMargins(24, 0, 0, 0)
            path_label = QLabel(_("Backup location:"))
            path_row.addWidget(path_label)
            self._lbl_path = QLabel(str(self._backup_path))
            self._lbl_path.setObjectName("hintLabel")
            self._lbl_path.setToolTip(str(self._backup_path))
            path_row.addWidget(self._lbl_path, 1)
            btn_change = QPushButton(_("Change\u2026"))
            btn_change.setFixedWidth(80)
            btn_change.clicked.connect(self._on_change_path)
            path_row.addWidget(btn_change)

            path_container = QWidget()
            path_container.setLayout(path_row)
            layout.addWidget(path_container)

            # Toggle path row visibility with checkbox
            self._chk_backup.toggled.connect(path_container.setVisible)

            # Schedule checkbox (only if not already enabled)
            if not schedule_enabled:
                self._chk_schedule = QCheckBox(_("Enable scheduled backups"))
                layout.addWidget(self._chk_schedule)

        layout.addSpacing(8)

        # --- Footer ---
        footer = QLabel(
            _("Full changelog available in Help \u2192 About \u2192 News")
        )
        footer.setObjectName("hintLabel")
        footer.setWordWrap(True)
        layout.addWidget(footer)

        scroll.setWidget(content)
        outer_layout.addWidget(scroll, 1)

        # --- Buttons (outside scroll, always visible) ---
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_download = QPushButton(_("Download"))
        btn_dismiss_version = QPushButton(_("Skip version"))
        btn_dismiss = QPushButton(_("Dismiss"))
        btn_row.addWidget(btn_download)
        btn_row.addWidget(btn_dismiss_version)
        btn_row.addWidget(btn_dismiss)
        outer_layout.addLayout(btn_row)

        btn_download.clicked.connect(self._on_download)
        btn_dismiss_version.clicked.connect(self._on_dismiss_version)
        btn_dismiss.clicked.connect(self.reject)

    def _on_change_path(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, _("Select Backup Location"), str(self._backup_path)
        )
        if chosen:
            self._backup_path = Path(chosen)
            self._path_changed = True
            self._lbl_path.setText(str(self._backup_path))
            self._lbl_path.setToolTip(str(self._backup_path))

    def _on_download(self) -> None:
        self._action = "download"
        self.accept()

    def _on_dismiss_version(self) -> None:
        self._action = "dismiss_version"
        self.accept()

    # --- Public properties for the caller ---

    @property
    def action(self) -> str:
        """User's chosen action: 'download', 'dismiss_version', or 'dismiss'."""
        return self._action

    @property
    def should_backup(self) -> bool:
        """Whether the user wants a pre-update backup."""
        return bool(self._chk_backup and self._chk_backup.isChecked())

    @property
    def backup_path(self) -> Optional[Path]:
        """The backup path (possibly changed by the user)."""
        return self._backup_path

    @property
    def path_changed(self) -> bool:
        """Whether the user changed the backup path."""
        return self._path_changed

    @property
    def enable_schedule(self) -> bool:
        """Whether the user opted into scheduled backups."""
        return bool(self._chk_schedule and self._chk_schedule.isChecked())
