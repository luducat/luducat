# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# config_editor.py

"""Text editor dialog for platform configuration files.

Shared by DOSBox and ScummVM for editing .conf / ScummVM config files.
Uses QPlainTextEdit with monospace font and standard button layout.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QPlainTextEdit,
    QDialogButtonBox, QPushButton,
)
from PySide6.QtGui import QFont, QFontDatabase, QDesktopServices
from PySide6.QtCore import QUrl



class ConfigEditorDialog(QDialog):
    """Text editor for platform configuration files.

    Features:
    - QPlainTextEdit with monospace font
    - Save / Cancel (standard buttons, Qt-translated)
    - Optional Help button → opens doc URL in browser
    - Config type label for context
    """

    def __init__(
        self,
        title: str,
        config_text: str,
        doc_url: str = "",
        read_only: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("configEditor")
        self.setWindowTitle(title)
        self.setMinimumSize(600, 400)
        self.resize(700, 500)

        self._doc_url = doc_url
        self._result_text = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Title label
        title_label = QLabel(title)
        title_label.setObjectName("configEditorTitle")
        layout.addWidget(title_label)

        # Text editor
        self._editor = QPlainTextEdit()
        self._editor.setPlainText(config_text)
        self._editor.setReadOnly(read_only)

        # Monospace font
        mono_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self._editor.setFont(mono_font)

        layout.addWidget(self._editor, 1)

        # Button box
        buttons = QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        self._button_box = QDialogButtonBox(buttons)
        self._button_box.accepted.connect(self._on_save)
        self._button_box.rejected.connect(self.reject)

        # Help button
        if doc_url:
            help_btn = QPushButton(_("Documentation"))
            help_btn.clicked.connect(self._on_help)
            self._button_box.addButton(
                help_btn, QDialogButtonBox.ButtonRole.HelpRole
            )

        if read_only:
            save_btn = self._button_box.button(
                QDialogButtonBox.StandardButton.Save
            )
            if save_btn:
                save_btn.setEnabled(False)

        layout.addWidget(self._button_box)

    def get_text(self):
        """Get the edited text.

        Returns:
            Edited text string if saved, None if cancelled
        """
        return self._result_text

    def _on_save(self):
        self._result_text = self._editor.toPlainText()
        self.accept()

    def _on_help(self):
        if self._doc_url:
            QDesktopServices.openUrl(QUrl(self._doc_url))
