# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# icons.py

"""Shared SVG icon tinting utilities.

Provides a single function for loading SVG icons and recoloring them
to match the current palette. Replaces duplicated composition patterns
across category_sidebar, author_list, and filter_bar.
"""

from pathlib import Path
from typing import Optional, Union

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication

_ICONS_DIR = Path(__file__).parent.parent / "assets" / "icons"


def load_tinted_icon(
    svg_path: Union[str, Path],
    size: int = 16,
    color: Optional[QColor] = None,
) -> QIcon:
    """Load an SVG and tint it to a solid color.

    Uses QPainter composition to recolor the icon. If no color is given,
    uses the current palette's WindowText color.

    Args:
        svg_path: Either an absolute path, or a filename relative to
                  luducat/assets/icons/ (e.g. "dice.svg").
        size: Pixel size to scale the icon to (square).
        color: Override tint color. Defaults to palette WindowText.

    Returns:
        QIcon with the tinted pixmap, or an empty QIcon on failure.
    """
    path = Path(svg_path)
    if not path.is_absolute():
        path = _ICONS_DIR / path

    source = QPixmap(str(path))
    if source.isNull():
        return QIcon()

    source = source.scaled(
        QSize(size, size),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )

    if color is None:
        color = QApplication.palette().color(QPalette.ColorRole.WindowText)

    colored = QPixmap(source.size())
    colored.fill(Qt.GlobalColor.transparent)
    painter = QPainter(colored)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
    painter.drawPixmap(0, 0, source)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(colored.rect(), color)
    painter.end()
    return QIcon(colored)
