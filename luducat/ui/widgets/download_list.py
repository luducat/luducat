# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Download list model and game row delegate for the download window.

The model wraps DownloadManager.get_queue() data and tracks per-group
download speed. The delegate paints game rows with icon, title, status,
progress bar, and action button hit zones.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import (
    QAbstractListModel,
    QMimeData,
    QModelIndex,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

logger = logging.getLogger(__name__)

try:
    _("")
except NameError:
    def _(s): return s


# -- Helpers --

def _format_bytes(n: Optional[int]) -> str:
    """Human-readable byte size."""
    if n is None or n < 0:
        return "?"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _format_speed(bps: float) -> str:
    """Human-readable speed string."""
    if bps <= 0:
        return ""
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / (1024 * 1024):.1f} MB/s"


def _format_eta(speed_bps: float, remaining_bytes: int) -> str:
    if speed_bps <= 0 or remaining_bytes <= 0:
        return ""
    seconds = remaining_bytes / speed_bps
    if seconds < 60:
        return _("< 1 min")
    minutes = int(seconds / 60)
    if minutes < 60:
        return _("~{n} min").format(n=minutes)
    hours = minutes // 60
    mins = minutes % 60
    return _("~{h}h {m}m").format(h=hours, m=mins)


# Custom data roles
GROUP_ROLE = Qt.ItemDataRole.UserRole + 1
SPEED_ROLE = Qt.ItemDataRole.UserRole + 2
STORE_ROLE = Qt.ItemDataRole.UserRole + 3
ETA_ROLE = Qt.ItemDataRole.UserRole + 4

# Row dimensions
ROW_HEIGHT = 72
ICON_SIZE = 36
BADGE_DRAW_SIZE = 28
BADGE_WIDTH = int(BADGE_DRAW_SIZE * 1.6)
LEFT_BORDER_W = 3
PADDING = 8

_DEFAULT_BRAND = {"bg": "#2a2a2a", "text": "#ffffff"}


# -- Model --


class DownloadListModel(QAbstractListModel):
    """List model backed by DownloadManager.get_queue() snapshots.

    Call refresh(groups) periodically from a QTimer. The model
    tracks per-group speed by remembering previous bytes_downloaded.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._groups: list[dict] = []
        self._prev_bytes: dict[str, int] = {}
        self._speeds: dict[str, float] = {}
        self._last_refresh: float = 0.0

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        default = super().flags(index)
        if index.isValid():
            return (default | Qt.ItemFlag.ItemIsDragEnabled
                    | Qt.ItemFlag.ItemIsDropEnabled)
        return default | Qt.ItemFlag.ItemIsDropEnabled

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def mimeTypes(self) -> list[str]:
        return ["application/x-luducat-download-row"]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        mime = QMimeData()
        if indexes:
            row = indexes[0].row()
            group = self._groups[row] if 0 <= row < len(self._groups) else None
            if group:
                mime.setData("application/x-luducat-download-row",
                             group["id"].encode("utf-8"))
        return mime

    def dropMimeData(self, data: QMimeData, action, row: int, column: int,
                     parent: QModelIndex) -> bool:
        if action != Qt.DropAction.MoveAction:
            return False
        raw = data.data("application/x-luducat-download-row")
        if not raw:
            return False
        group_id = bytes(raw).decode("utf-8")
        target = row if row >= 0 else self.rowCount()
        try:
            from luducat.core.download_manager import get_download_manager
            dm = get_download_manager()
            dm.move_group_to(group_id, target)
        except RuntimeError:
            return False
        return True

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._groups)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._groups):
            return None

        group = self._groups[index.row()]

        if role == Qt.ItemDataRole.DisplayRole:
            return group.get("game_title", "")
        if role == GROUP_ROLE:
            return group
        if role == SPEED_ROLE:
            return self._speeds.get(group["id"], 0.0)
        if role == STORE_ROLE:
            return group.get("store_name", "")
        if role == ETA_ROLE:
            speed = self._speeds.get(group["id"], 0.0)
            total = group.get("total_bytes", 0) or 0
            downloaded = group.get("downloaded_bytes", 0) or 0
            remaining = total - downloaded
            return _format_eta(speed, remaining)
        if role == Qt.ItemDataRole.ToolTipRole:
            status = group.get("status", "")
            dl = _format_bytes(group.get("downloaded_bytes"))
            total = _format_bytes(group.get("total_bytes"))
            fc = group.get("files_completed", 0)
            ft = group.get("file_count", 0)
            return f"{status} \u2014 {dl} / {total} \u2014 {fc}/{ft} files"

        return None

    def group_at(self, row: int) -> Optional[dict]:
        if 0 <= row < len(self._groups):
            return self._groups[row]
        return None

    def speed_for(self, group_id: str) -> float:
        return self._speeds.get(group_id, 0.0)

    def total_speed(self) -> float:
        return sum(self._speeds.values())

    def refresh(self, groups: list[dict]) -> None:
        """Update model with fresh group data from DownloadManager."""
        now = time.monotonic()
        elapsed = now - self._last_refresh if self._last_refresh > 0 else 0.0

        # Calculate per-group speeds
        if elapsed > 0.1:
            new_speeds: dict[str, float] = {}
            for g in groups:
                gid = g["id"]
                cur_bytes = g.get("downloaded_bytes", 0) or 0
                prev = self._prev_bytes.get(gid, cur_bytes)
                delta = max(0, cur_bytes - prev)
                new_speeds[gid] = delta / elapsed
            self._speeds = new_speeds

        # Update previous bytes snapshot
        self._prev_bytes = {
            g["id"]: g.get("downloaded_bytes", 0) or 0 for g in groups
        }
        self._last_refresh = now

        self.beginResetModel()
        self._groups = list(groups)
        self.endResetModel()


# -- Delegate --


def _make_gradient(base_color: QColor, y: float, h: float) -> QLinearGradient:
    lighter = QColor(base_color)
    lighter.setRed(min(255, lighter.red() + 30))
    lighter.setGreen(min(255, lighter.green() + 30))
    lighter.setBlue(min(255, lighter.blue() + 30))
    darker = QColor(base_color)
    darker.setRed(max(0, darker.red() - 20))
    darker.setGreen(max(0, darker.green() - 20))
    darker.setBlue(max(0, darker.blue() - 20))
    grad = QLinearGradient(0, y, 0, y + h)
    grad.setColorAt(0.0, lighter)
    grad.setColorAt(0.45, base_color)
    grad.setColorAt(1.0, darker)
    return grad


class GameRowDelegate(QStyledItemDelegate):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._brand_colors: dict[str, dict[str, str]] = {}
        self._badge_labels: dict[str, str] = {}
        self._dl_colors: dict[str, str] = {}

    def set_download_colors(self, colors: dict[str, str]) -> None:
        self._dl_colors.update(colors)

    def _state_color(self, status: str, pal: QPalette) -> QColor:
        """Resolve a download status to an accent color from theme or palette."""
        if status in ("DOWNLOADING", "COMPLETED"):
            c = self._dl_colors.get("completed")
            if c:
                return QColor(c)
            return pal.color(QPalette.ColorRole.Highlight)
        if status in ("FAILED", "CANCELLED"):
            c = self._dl_colors.get("failed")
            if c:
                return QColor(c)
            return pal.color(QPalette.ColorRole.Highlight)
        if status == "PAUSED":
            c = self._dl_colors.get("paused")
            if c:
                return QColor(c)
            return pal.color(QPalette.ColorRole.Mid)
        if status == "RESOLVING":
            return pal.color(QPalette.ColorRole.Link)
        return pal.color(QPalette.ColorRole.PlaceholderText)

    def set_brand_colors(
        self,
        brand_colors: dict[str, dict[str, str]],
        badge_labels: dict[str, str],
    ) -> None:
        self._brand_colors = dict(brand_colors)
        self._badge_labels = dict(badge_labels)

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = option.rect
        pal = option.palette

        group = index.data(GROUP_ROLE)
        if group is None:
            painter.restore()
            return

        speed = index.data(SPEED_ROLE) or 0.0
        eta = index.data(ETA_ROLE) or ""
        status = group.get("status", "PENDING")
        title = group.get("game_title", "")
        store = group.get("store_name", "")
        dl_bytes = group.get("downloaded_bytes", 0) or 0
        total_bytes = group.get("total_bytes", 0) or 0
        fc = group.get("files_completed", 0) or 0
        ft = group.get("file_count", 0) or 0

        is_resolving = status == "RESOLVING"
        is_completed = status == "COMPLETED"
        is_downloading = status == "DOWNLOADING"
        is_failed = status in ("FAILED", "CANCELLED")
        is_paused = status == "PAUSED"
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)

        # -- Completed rows dimmed --
        if is_completed:
            painter.setOpacity(0.7)

        # -- Left state border --
        border_color = self._state_color(status, pal)
        if is_selected:
            left_border_color = pal.color(QPalette.ColorRole.Highlight)
        else:
            left_border_color = border_color
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(left_border_color)
        painter.drawRect(QRectF(
            rect.x(), rect.y(), LEFT_BORDER_W, rect.height(),
        ))

        # -- Row background tint --
        tint = QColor(border_color)
        tint.setAlpha(15)
        painter.fillRect(
            QRectF(rect.x() + LEFT_BORDER_W, rect.y(),
                   rect.width() - LEFT_BORDER_W, rect.height()),
            tint,
        )

        # -- Selection highlight (over tint) --
        if is_selected:
            sel = QColor(pal.color(QPalette.ColorRole.Highlight))
            sel.setAlpha(90)
            painter.fillRect(rect, sel)

        # -- Bottom separator --
        painter.setPen(QPen(pal.color(QPalette.ColorRole.Mid), 1))
        painter.drawLine(rect.x(), rect.bottom(), rect.right(), rect.bottom())

        text_color = pal.color(QPalette.ColorRole.Text)

        x = rect.x() + LEFT_BORDER_W + PADDING
        y = rect.y()

        # -- Store badge (wider rect to avoid text clipping) --
        badge_y = y + (ROW_HEIGHT - BADGE_DRAW_SIZE) // 2
        from luducat.ui.badge_painter import draw_store_icon_badge
        from PySide6.QtCore import QRect as _QRect
        badge_rect = _QRect(
            int(x), badge_y, BADGE_WIDTH, BADGE_DRAW_SIZE,
        )
        colors = self._brand_colors.get(store, _DEFAULT_BRAND)
        label = self._badge_labels.get(store, store.upper()[:3])
        draw_store_icon_badge(
            painter, badge_rect, store,
            colors.get("bg", _DEFAULT_BRAND["bg"]),
            colors.get("text", _DEFAULT_BRAND["text"]),
            badge_label=label, radius=3,
        )

        base_size = QApplication.instance().font().pointSize()
        if base_size <= 0:
            base_size = 10

        # -- Queue position pill (top-right of badge area) --
        if status in ("PENDING", "DOWNLOADING", "PAUSED"):
            queue_num = f"#{index.row() + 1}"
            q_font = QFont(painter.font())
            q_font.setPointSize(max(6, base_size - 3))
            q_font.setBold(True)
            painter.setFont(q_font)
            qfm = painter.fontMetrics()
            qtw = qfm.horizontalAdvance(queue_num) + 6
            qth = qfm.height() + 2
            q_rect = QRectF(
                x + BADGE_WIDTH - qtw + 2, y + 4, qtw, qth)
            pill_bg = QColor(pal.color(QPalette.ColorRole.WindowText))
            pill_bg.setAlpha(140)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(pill_bg)
            painter.drawRoundedRect(q_rect, 3, 3)
            painter.setPen(pal.color(QPalette.ColorRole.Window))
            painter.drawText(
                q_rect, Qt.AlignmentFlag.AlignCenter, queue_num)

        # -- Text area (full width, no button column) --
        text_x = x + BADGE_WIDTH + PADDING
        text_w = rect.right() - PADDING - text_x

        # Status text (right-aligned in title row)
        _status_texts = {
            "RESOLVING": _("Resolving..."),
            "COMPLETED": _("Done"),
            "DOWNLOADING": _("Downloading"),
            "PAUSED": _("Paused"),
            "PENDING": _("Queued"),
            "FAILED": _("Failed"),
            "CANCELLED": _("Cancelled"),
        }
        status_text = _status_texts.get(status, _("Queued"))
        has_accent = status not in ("PAUSED", "PENDING")

        status_font = QFont(painter.font())
        status_font.setPointSize(max(7, base_size - 1))
        painter.setFont(status_font)
        sfm = painter.fontMetrics()
        status_tw = sfm.horizontalAdvance(status_text) + 4

        # Verification badge for completed groups
        verified = group.get("verified")
        verify_tw = 0
        if is_completed and verified:
            if verified == "ok":
                v_icon = "✓"
                v_color = self._state_color("COMPLETED", pal)
            elif verified == "size_only":
                v_icon = "✓"
                v_color = pal.color(QPalette.ColorRole.PlaceholderText)
            else:
                v_icon = "✗"
                v_color = self._state_color("FAILED", pal)
            v_tw = sfm.horizontalAdvance(v_icon) + 4
            verify_tw = v_tw
            painter.setPen(v_color)
            v_rect = QRectF(
                text_x + text_w - status_tw - v_tw, y + 6, v_tw, 20)
            painter.drawText(
                v_rect,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                v_icon,
            )

        if has_accent:
            painter.setPen(self._state_color(status, pal))
        else:
            painter.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
        status_rect = QRectF(
            text_x + text_w - status_tw, y + 6, status_tw, 20)
        painter.drawText(
            status_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            status_text,
        )

        # Title (left-aligned, clipped before status text)
        title_avail = text_w - status_tw - verify_tw - 8
        title_font = QFont(painter.font())
        title_font.setPointSize(base_size + 1)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(text_color)
        title_rect = QRectF(text_x, y + 6, title_avail, 20)
        fm = painter.fontMetrics()
        elided = fm.elidedText(
            title, Qt.TextElideMode.ElideRight, int(title_avail))
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            elided,
        )

        # Second line
        sub_font = QFont(painter.font())
        sub_font.setPointSize(max(7, base_size - 1))
        sub_font.setBold(False)
        painter.setFont(sub_font)

        if is_resolving:
            painter.setPen(self._state_color("RESOLVING", pal))
            sub_text = store.upper() if store else _("Resolving...")
        elif is_downloading and speed > 0:
            painter.setPen(self._state_color("DOWNLOADING", pal))
            sub_text = "\u2193 " + _format_speed(speed)
            if eta:
                sub_text += " \u2014 " + eta
        elif is_failed and group.get("last_error"):
            painter.setPen(self._state_color("FAILED", pal))
            sub_text = group["last_error"]
        elif is_failed:
            painter.setPen(self._state_color("FAILED", pal))
            sub_text = _("{done} of {total} files").format(done=fc, total=ft)
        else:
            painter.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
            sub_text = _("{done} of {total} files").format(done=fc, total=ft)

        sub_rect = QRectF(text_x, y + 26, text_w, 16)
        painter.drawText(
            sub_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            sub_text,
        )

        # -- Progress bar --
        bar_y = y + 44
        bar_h = 16
        bar_w = text_w
        bar_rect = QRectF(text_x, bar_y, bar_w, bar_h)

        # Bar background gradient
        bg_grad = _make_gradient(
            pal.color(QPalette.ColorRole.Button), bar_y, bar_h,
        )
        painter.setPen(QPen(pal.color(QPalette.ColorRole.Mid), 1))
        painter.setBrush(bg_grad)
        painter.drawRoundedRect(bar_rect, 3, 3)

        # Fill
        frac = 0.0
        if total_bytes > 0:
            frac = min(1.0, dl_bytes / total_bytes)
        if frac > 0:
            fill_rect = QRectF(text_x, bar_y, bar_w * frac, bar_h)
            base = self._state_color(status, pal)
            fill_grad = _make_gradient(base, bar_y, bar_h)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill_grad)
            painter.drawRoundedRect(fill_rect, 3, 3)

        # Bar label
        bar_font = QFont(painter.font())
        bar_font.setPointSize(max(7, base_size - 2))
        painter.setFont(bar_font)
        pct = int(frac * 100)
        if is_resolving:
            bar_text = _("Resolving...")
        elif is_completed:
            bar_text = f"{_format_bytes(total_bytes)} \u2014 " + _("Complete")
        elif is_failed:
            bar_text = (f"{_format_bytes(dl_bytes)} / "
                        f"{_format_bytes(total_bytes)} \u2014 " + _("Failed"))
        elif status == "PENDING":
            bar_text = _("Queued") + f" \u2014 {_format_bytes(total_bytes)}"
        elif is_paused:
            bar_text = (f"{_format_bytes(dl_bytes)} / "
                        f"{_format_bytes(total_bytes)} \u2014 " + _("Paused"))
        else:
            bar_text = (f"{_format_bytes(dl_bytes)} / "
                        f"{_format_bytes(total_bytes)} \u2014 {pct}%")

        if pct > 55:
            painter.setPen(pal.color(QPalette.ColorRole.HighlightedText))
        else:
            painter.setPen(text_color)
        painter.drawText(bar_rect, Qt.AlignmentFlag.AlignCenter, bar_text)

        painter.restore()
