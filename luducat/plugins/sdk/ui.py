# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# ui.py

"""SDK UI helpers for plugin dialogs.

Provides common dialog patterns following luducat's theming conventions.
Plugin authors use these instead of guessing at QSS object names and
layout patterns.

Self-contained for simple helpers (QMessageBox wrappers, form groups).
Delegates to core via ``_registry`` for icon tinting.

Usage in plugins::

    from luducat.plugins.sdk.ui import (
        create_form_group, create_status_label,
        show_confirmation, show_error, show_info,
        load_tinted_icon,
    )
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple, Union

from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QWidget,
)

from . import _registry


def create_status_label() -> QLabel:
    """Create a themed status label with proper QSS object name.

    Returns a QLabel with objectName="statusLabel" for theme styling.
    """
    label = QLabel()
    label.setObjectName("statusLabel")
    return label


def create_form_group(title: str) -> Tuple[QGroupBox, QFormLayout]:
    """Create a settings group with form layout (standard pattern).

    Args:
        title: Group box title text.

    Returns:
        (group_box, form_layout) tuple ready to add rows to.
    """
    group = QGroupBox(title)
    layout = QFormLayout(group)
    return group, layout


def show_confirmation(parent: QWidget, title: str, message: str) -> bool:
    """Themed confirmation dialog.

    Args:
        parent: Parent widget.
        title: Dialog title.
        message: Dialog message.

    Returns:
        True if user clicked Yes.
    """
    result = QMessageBox.question(
        parent, title, message,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return result == QMessageBox.StandardButton.Yes


def show_error(parent: QWidget, title: str, message: str) -> None:
    """Themed error dialog.

    Args:
        parent: Parent widget.
        title: Dialog title.
        message: Error message.
    """
    QMessageBox.critical(parent, title, message)


def show_info(parent: QWidget, title: str, message: str) -> None:
    """Themed info dialog.

    Args:
        parent: Parent widget.
        title: Dialog title.
        message: Info message.
    """
    QMessageBox.information(parent, title, message)


def open_url(url: str) -> None:
    """Open a URL in the user's preferred browser.

    Delegates to ``utils.browser.open_url`` via registry.
    Falls back to ``QDesktopServices.openUrl()`` if the registry
    slot is not populated.

    Args:
        url: The URL to open.
    """
    fn = _registry._open_url
    if fn is not None:
        fn(url)
        return
    # Fallback: QDesktopServices
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl(url))


def load_tinted_icon(
    svg_path: Union[str, Path],
    size: int = 16,
    color: Optional[QColor] = None,
) -> QIcon:
    """Load an SVG icon with palette-aware tinting.

    Delegates to ``utils.icons.load_tinted_icon`` via registry.

    Args:
        svg_path: Filename relative to assets/icons/ or absolute path.
        size: Pixel size to scale the icon to (square).
        color: Override tint color. Defaults to palette WindowText.

    Returns:
        QIcon with the tinted pixmap, or an empty QIcon on failure.
    """
    fn = _registry._load_tinted_icon
    if fn is None:
        return QIcon()
    return fn(svg_path, size=size, color=color)
