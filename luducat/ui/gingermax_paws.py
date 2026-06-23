# This file is part of the luducat Project.
# License: GPL-3.0-or-later. Contact: luducat-project@trinity2k.net
#

from PySide6.QtWidgets import QGroupBox, QStyleOptionGroupBox, QStyle, QApplication
from PySide6.QtGui import QPainter, QColor, QPalette
from PySide6.QtCore import Qt, QRectF

_MARKER = "gingermax-paws"
_FALLBACK_ACCENT = "#E2912A"
_orig_paintEvent = QGroupBox.paintEvent
_config_allowed = True


def set_config_allowed(allowed: bool) -> None:
    """Toggle paw accents at runtime (called from settings)."""
    global _config_allowed
    _config_allowed = allowed


def _enabled() -> bool:
    if not _config_allowed:
        return False
    app = QApplication.instance()
    return bool(app) and _MARKER in (app.styleSheet() or "")


def _accent() -> QColor:
    app = QApplication.instance()
    if app is not None:
        c = app.palette().color(QPalette.Highlight)
        if c.isValid():
            return c
    return QColor(_FALLBACK_ACCENT)


def _draw_paw(p: QPainter, x: float, y: float, h: float, color: QColor) -> None:
    """Draw a 5-ellipse paw print (28x26 design space) scaled to height h."""
    s = h / 26.0
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    for cx, cy, rx, ry in (
        (14.0, 18.0, 7.4, 6.0),    # main pad
        (5.6, 11.0, 2.6, 3.3),     # toe beans
        (10.8, 6.6, 2.7, 3.5),
        (17.2, 6.6, 2.7, 3.5),
        (22.4, 11.0, 2.6, 3.3),
    ):
        p.drawEllipse(QRectF(x + (cx - rx) * s, y + (cy - ry) * s, 2 * rx * s, 2 * ry * s))
    p.restore()


def _paw_paintEvent(self: QGroupBox, event) -> None:
    eligible = _enabled() and not self.isCheckable()

    # Remember the author-set title once; keep the displayed title in sync with the
    # enabled state (reserve leading room for the paw only while enabled, so other
    # themes are visually untouched).
    orig = self.property("_pawOrigTitle")
    if orig is None:
        orig = self.title()
        self.setProperty("_pawOrigTitle", orig)
    desired = ("     " + orig) if (eligible and orig) else orig
    if self.title() != desired:
        self.setTitle(desired)

    _orig_paintEvent(self, event)

    if not (eligible and orig):
        return

    opt = QStyleOptionGroupBox()
    self.initStyleOption(opt)
    r = self.style().subControlRect(QStyle.CC_GroupBox, opt, QStyle.SC_GroupBoxLabel, self)
    h = max(11.0, min(16.0, float(r.height()) - 4.0))
    p = QPainter(self)
    _draw_paw(p, float(r.left()) + 1.0, float(r.top()) + (float(r.height()) - h) / 2.0, h, _accent())
    p.end()


def install() -> None:
    """Enable paw-print group titles (idempotent)."""
    QGroupBox.paintEvent = _paw_paintEvent


def uninstall() -> None:
    """Restore the original QGroupBox.paintEvent."""
    QGroupBox.paintEvent = _orig_paintEvent
