# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Download companion window -- floating drop target with download progress.

A top-level QWidget (not QDialog) that sits alongside the main window.
Accepts URL drops, resolves them via store handlers, and shows download
progress using the DownloadManager singleton.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QRect, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QGuiApplication,
    QPainter,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from luducat.core.constants import APP_NAME
from luducat.core.download_manager import get_download_manager
from luducat.utils.icons import load_tinted_icon
from luducat.ui.widgets.download_list import (
    DownloadListModel,
    GameRowDelegate,
    GROUP_ROLE,
    _format_speed,
)

logger = logging.getLogger(__name__)

try:
    _("")
except NameError:
    def _(s): return s


POLL_INTERVAL_MS = 500
DEFAULT_WIDTH = 480
DEFAULT_HEIGHT = 620


# ─── Notification Banner (stub for 0.9+) ─────────────────────────────


class _NotificationBanner(QWidget):
    """Banner showing new games and available updates."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("downloadNotificationBanner")
        self.hide()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(8)

        self._new_pill = QLabel()
        self._new_pill.setObjectName("newGamesPill")
        self._new_pill.hide()
        layout.addWidget(self._new_pill)

        self._updates_pill = QLabel()
        self._updates_pill.setObjectName("updatesPill")
        self._updates_pill.hide()
        layout.addWidget(self._updates_pill)

        layout.addStretch()

        self._queue_btn = QPushButton(_("Queue all updates"))
        self._queue_btn.setObjectName("queueAllUpdatesBtn")
        self._queue_btn.setFlat(True)
        self._queue_btn.hide()
        layout.addWidget(self._queue_btn)

    def set_notification(self, new_count: int = 0,
                         update_count: int = 0) -> None:
        if new_count > 0:
            self._new_pill.setText(
                _("{n} NEW").format(n=new_count))
            self._new_pill.show()
        else:
            self._new_pill.hide()

        if update_count > 0:
            self._updates_pill.setText(
                _("{n} game updates").format(n=update_count))
            self._updates_pill.show()
            self._queue_btn.show()
        else:
            self._updates_pill.hide()
            self._queue_btn.hide()

        self.setVisible(new_count > 0 or update_count > 0)


# ─── Status Footer ───────────────────────────────────────────────────


class _StatusFooter(QWidget):
    """Bottom status bar with breakdown counts and speed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("downloadStatusFooter")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 2, 12, 2)
        layout.setSpacing(8)

        self._dot = QLabel("●")
        self._dot.setObjectName("downloadStatusDot")
        layout.addWidget(self._dot)

        self._breakdown = QLabel("")
        self._breakdown.setObjectName("hintLabel")
        layout.addWidget(self._breakdown, 1)

        self._speed = QLabel("")
        self._speed.setObjectName("hintLabel")
        layout.addWidget(self._speed)

    def update_status(self, groups: list[dict], total_speed: float) -> None:
        total = len(groups)
        finished = sum(1 for g in groups if g.get("status") == "COMPLETED")
        downloading = sum(1 for g in groups
                          if g.get("status") == "DOWNLOADING")
        paused = sum(1 for g in groups if g.get("status") == "PAUSED")
        queued = sum(1 for g in groups if g.get("status") == "PENDING")
        failed = sum(1 for g in groups if g.get("status") == "FAILED")
        cancelled = sum(1 for g in groups
                        if g.get("status") == "CANCELLED")

        parts = [_("{n} total").format(n=total)]
        if finished:
            parts.append(_("{n} finished").format(n=finished))
        if downloading:
            parts.append(_("{n} downloading").format(n=downloading))
        if paused:
            parts.append(_("{n} paused").format(n=paused))
        if queued:
            parts.append(_("{n} queued").format(n=queued))
        if failed:
            parts.append(_("{n} failed").format(n=failed))
        if cancelled:
            parts.append(_("{n} cancelled").format(n=cancelled))
        self._breakdown.setText(" · ".join(parts))

        self._speed.setText(_format_speed(total_speed) if total_speed > 0
                            else "")


# ─── Drop Overlay ────────────────────────────────────────────────────


class _DropOverlay(QWidget):
    """Overlay shown on the list viewport during drag-enter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("downloadDropOverlay")
        self.hide()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        pal = self.palette()
        accent = pal.color(QPalette.ColorRole.Highlight)

        # Dashed border
        pen = QPen(accent, 2, Qt.PenStyle.DashLine)
        p.setPen(pen)
        tint = QColor(accent)
        tint.setAlpha(25)
        p.setBrush(tint)
        p.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 4, 4)

        # Centered pill
        pill_text = _("Drop to add")
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(pill_text)
        th = fm.height()
        pill_w = tw + 28
        pill_h = th + 8
        pill_rect = QRect(
            (self.width() - pill_w) // 2,
            (self.height() - pill_h) // 2,
            pill_w, pill_h,
        )
        p.setPen(QPen(accent, 1))
        bg = pal.color(QPalette.ColorRole.Base)
        p.setBrush(bg)
        p.drawRoundedRect(pill_rect, 3, 3)

        p.setPen(accent)
        p.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, pill_text)
        p.end()


# ─── Download Window ─────────────────────────────────────────────────


class DownloadWindow(QWidget):
    """Floating download companion window."""

    settings_requested = Signal(str)

    def __init__(self, config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self._config = config
        self._resolve_worker = None

        self.setWindowTitle(_("{app} Downloader").format(app=APP_NAME))
        self.setMinimumWidth(380)
        self.setMinimumHeight(400)
        self.setObjectName("downloadWindow")
        self.setAcceptDrops(True)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll)

        self._setup_ui()
        self._restore_geometry()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 1. Notification banner (hidden stub)
        self._banner = _NotificationBanner()
        layout.addWidget(self._banner)

        # Content area with padding
        content = QVBoxLayout()
        content.setContentsMargins(12, 8, 12, 8)
        content.setSpacing(8)

        # 2. Control bar
        ctrl = QHBoxLayout()
        ctrl.setSpacing(4)

        style = self.style()

        self._btn_move_up = QPushButton()
        self._btn_move_up.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_ArrowUp))
        self._btn_move_up.setToolTip(
            _("Move selected up in queue"))
        self._btn_move_up.setFixedWidth(32)
        self._btn_move_up.clicked.connect(self._on_move_up)
        ctrl.addWidget(self._btn_move_up)

        self._btn_move_down = QPushButton()
        self._btn_move_down.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
        self._btn_move_down.setToolTip(
            _("Move selected down in queue"))
        self._btn_move_down.setFixedWidth(32)
        self._btn_move_down.clicked.connect(self._on_move_down)
        ctrl.addWidget(self._btn_move_down)

        self._btn_enqueue = QPushButton()
        self._btn_enqueue.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self._btn_enqueue.setToolTip(
            _("Start selected (Shift: all pending)"))
        self._btn_enqueue.setFixedWidth(32)
        self._btn_enqueue.clicked.connect(self._on_enqueue)
        ctrl.addWidget(self._btn_enqueue)

        self._btn_pause = QPushButton()
        self._btn_pause.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaPause))
        self._btn_pause.setToolTip(
            _("Pause selected (Shift: pause all)"))
        self._btn_pause.setFixedWidth(32)
        self._btn_pause.clicked.connect(self._on_pause)
        ctrl.addWidget(self._btn_pause)

        self._btn_cancel = QPushButton()
        self._btn_cancel.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self._btn_cancel.setToolTip(
            _("Cancel selected (Shift: cancel all)"))
        self._btn_cancel.setFixedWidth(32)
        self._btn_cancel.clicked.connect(self._on_cancel)
        ctrl.addWidget(self._btn_cancel)

        self._btn_clear = QPushButton(_("Clear"))
        self._btn_clear.setToolTip(
            _("Clear finished downloads (Shift: cancel and remove all)"))
        self._btn_clear.clicked.connect(self._on_clear)
        ctrl.addWidget(self._btn_clear)

        ctrl.addStretch()

        self._btn_settings = QPushButton(_("Settings"))
        self._btn_settings.setToolTip(
            _("Open download settings"))
        self._btn_settings.clicked.connect(
            lambda: self.settings_requested.emit("Downloads"))
        ctrl.addWidget(self._btn_settings)

        self._btn_pin = QPushButton()
        self._update_pin_icon()
        self._btn_pin.setToolTip(_("Pin window on top of other windows"))
        self._btn_pin.setFixedWidth(32)
        self._btn_pin.setCheckable(True)
        self._btn_pin.setObjectName("pinButton")
        self._btn_pin.toggled.connect(self._on_pin_toggled)
        ctrl.addWidget(self._btn_pin)
        content.addLayout(ctrl)

        # 3. Download list
        self._model = DownloadListModel()
        self._delegate = GameRowDelegate()
        self._list_view = QListView()
        self._list_view.setModel(self._model)
        self._list_view.setItemDelegate(self._delegate)
        self._list_view.setSelectionMode(
            QListView.SelectionMode.SingleSelection)
        self._list_view.setVerticalScrollMode(
            QListView.ScrollMode.ScrollPerPixel)
        self._list_view.setAcceptDrops(True)
        self._list_view.setDragEnabled(True)
        self._list_view.setDragDropMode(
            QListView.DragDropMode.InternalMove)
        self._list_view.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._list_view.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._list_view.customContextMenuRequested.connect(
            self._on_context_menu)
        content.addWidget(self._list_view, 1)

        # Drop overlay (child of list viewport)
        self._drop_overlay = _DropOverlay(self._list_view.viewport())

        # Empty state label
        self._empty_label = QLabel(
            _("No downloads — drag links here or paste a URL below"))
        self._empty_label.setObjectName("hintLabel")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_label.hide()
        content.addWidget(self._empty_label)

        # 4. URL input row (bottom)
        input_row = QHBoxLayout()
        input_row.setSpacing(6)
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText(_("Paste URL here..."))
        self._url_input.returnPressed.connect(self._on_url_submit)
        input_row.addWidget(self._url_input, 1)

        self._btn_add = QPushButton(_("Add"))
        self._btn_add.setToolTip(_("Add URL to download queue"))
        self._btn_add.clicked.connect(self._on_url_submit)
        input_row.addWidget(self._btn_add)
        content.addLayout(input_row)

        layout.addLayout(content, 1)

        # 5. Status footer
        self._footer = _StatusFooter()
        layout.addWidget(self._footer)

        # Wire selection model
        self._list_view.selectionModel().currentChanged.connect(
            self._update_button_states)
        self._update_button_states()

    # -- Geometry persistence --

    def _restore_geometry(self) -> None:
        w = self._config.get("downloads.window_width", DEFAULT_WIDTH)
        h = self._config.get("downloads.window_height", DEFAULT_HEIGHT)
        self.resize(w, h)

        x = self._config.get("downloads.window_x", None)
        y = self._config.get("downloads.window_y", None)
        if x is not None and y is not None:
            screens = QGuiApplication.screens()
            for screen in screens:
                geom = screen.availableGeometry()
                if (geom.left() <= x < geom.right()
                        and geom.top() <= y < geom.bottom()):
                    self.move(x, y)
                    break

        if self._config.get("downloads.window_on_top", False):
            self._btn_pin.setChecked(True)

    def _save_geometry(self) -> None:
        self._config.set("downloads.window_width", self.width())
        self._config.set("downloads.window_height", self.height())
        pos = self.pos()
        self._config.set("downloads.window_x", pos.x())
        self._config.set("downloads.window_y", pos.y())

    def _update_pin_icon(self) -> None:
        from PySide6.QtCore import QSize
        icon = load_tinted_icon("pin.svg", size=18)
        if icon.isNull():
            self._btn_pin.setText("\U0001F4CC")
        else:
            self._btn_pin.setIcon(icon)
            self._btn_pin.setIconSize(QSize(18, 18))

    def _on_pin_toggled(self, checked: bool) -> None:
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()
        self._config.set("downloads.window_on_top", checked)

    # -- Lifecycle --

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._poll()
        self._poll_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._poll_timer.stop()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._save_geometry()
        self._poll_timer.stop()
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        vp = self._list_view.viewport()
        self._drop_overlay.setGeometry(vp.rect())

    # -- Drag and drop (whole window) --

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasText():
            event.acceptProposedAction()
            self._drop_overlay.setGeometry(self._list_view.viewport().rect())
            self._drop_overlay.show()
            self._drop_overlay.update()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:  # noqa: N802
        self._drop_overlay.hide()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self._drop_overlay.hide()

        mime = event.mimeData()
        urls: list[str] = []

        if mime.hasUrls():
            for qurl in mime.urls():
                url = qurl.toString()
                if url.startswith(("http://", "https://")):
                    urls.append(url)
        elif mime.hasText():
            text = mime.text().strip()
            for line in text.splitlines():
                line = line.strip()
                if line.startswith(("http://", "https://")):
                    urls.append(line)

        for url in urls:
            self._on_url_dropped(url)

        event.acceptProposedAction()

    # -- Polling --

    _poll_seq = 0

    def _poll(self) -> None:
        # import sys
        # DownloadWindow._poll_seq += 1
        # seq = DownloadWindow._poll_seq
        # timer_active = self._poll_timer.isActive()
        # print(f"TICK #{seq} timer={timer_active}", file=sys.stderr, flush=True)
        try:
            dm = get_download_manager()
        except RuntimeError:
            return

        groups = dm.get_queue()
        # active = [g for g in groups if g.get("status") not in ("COMPLETED", "CANCELLED")]
        # if active:
        #     for g in active:
        #         print(
        #             f"POLL {g.get('id', '?')[:8]}: "
        #             f"dl_bytes={g.get('downloaded_bytes')} "
        #             f"total={g.get('total_bytes')} "
        #             f"status={g.get('status')}",
        #             file=sys.stderr, flush=True,
        #         )

        selected_gid = None
        idx = self._list_view.currentIndex()
        if idx.isValid():
            group = idx.data(GROUP_ROLE)
            if group:
                selected_gid = group.get("id")

        self._model.refresh(groups)

        if selected_gid:
            for row in range(self._model.rowCount()):
                g = self._model.group_at(row)
                if g and g.get("id") == selected_gid:
                    self._list_view.setCurrentIndex(self._model.index(row))
                    break

        self._footer.update_status(groups, self._model.total_speed())

        self._empty_label.setVisible(len(groups) == 0)
        self._list_view.setVisible(len(groups) > 0)

        self._update_button_states()

    # -- URL handling --

    def _on_url_submit(self) -> None:
        url = self._url_input.text().strip()
        if url and url.startswith(("http://", "https://")):
            self._on_url_dropped(url)
            self._url_input.clear()

    def _on_url_dropped(self, url: str) -> None:
        from luducat.core.download_handlers import find_handler
        from luducat.ui.workers.resolve_worker import ResolveWorker

        handler = find_handler(url)
        if handler is None:
            truncated = url[:80] + ("..." if len(url) > 80 else "")
            QMessageBox.warning(
                self,
                _("Unsupported URL"),
                _("This URL is not supported by any download handler:\n\n"
                  "{url}").format(url=truncated),
            )
            return

        logger.debug("URL dropped: %s (handler: %s)", url[:80], handler.store_name)

        try:
            dm = get_download_manager()
            group_id = dm.create_resolving_group(url, handler.store_name)
        except Exception as e:
            logger.exception("Failed to create resolving group")
            QMessageBox.warning(self, _("Download Error"), str(e))
            return

        self._poll()

        self._url_input.setEnabled(False)
        self._btn_add.setEnabled(False)
        self._url_input.setPlaceholderText(
            _("Resolving {url}...").format(
                url=url[:50] + ("..." if len(url) > 50 else "")))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        worker = ResolveWorker(url=url, parent=self)
        worker.group_id = group_id
        worker.resolved.connect(self._on_resolved)
        worker.error.connect(self._on_resolve_error)
        worker.finished.connect(self._on_resolve_done)
        worker.finished.connect(worker.deleteLater)
        self._resolve_worker = worker
        worker.start()

    def _on_resolve_done(self) -> None:
        QApplication.restoreOverrideCursor()
        self._url_input.setEnabled(True)
        self._btn_add.setEnabled(True)
        self._url_input.setPlaceholderText(_("Paste URL here..."))
        self._resolve_worker = None
        self._poll()

    def _on_resolved(self, result: dict) -> None:
        group_id = getattr(self._resolve_worker, "group_id", None)

        if result["type"] == "target":
            target = result["target"]
            try:
                dm = get_download_manager()
                if group_id:
                    dm.finalize_resolving_group(group_id, target)
                else:
                    dm.submit(target)
            except Exception as e:
                QMessageBox.warning(self, _("Download Error"), str(e))

        elif result["type"] == "info":
            info = result["info"]
            handler = result["handler"]
            self._show_installer_picker(info, handler, group_id=group_id)

    def _on_resolve_error(self, message: str) -> None:
        group_id = getattr(self._resolve_worker, "group_id", None)
        if group_id:
            try:
                dm = get_download_manager()
                dm.fail_resolving_group(group_id, message)
            except Exception:
                logger.exception("Failed to mark resolving group as failed")
        else:
            QMessageBox.warning(self, _("Download Error"), message)

    def _show_installer_picker(self, info, handler, group_id=None) -> None:
        from luducat.ui.dialogs.installer_picker import InstallerPickerDialog
        from PySide6.QtWidgets import QDialog

        dialog = InstallerPickerDialog(info, parent=self, config=self._config)
        if dialog.exec_() != QDialog.DialogCode.Accepted:
            if group_id:
                try:
                    dm = get_download_manager()
                    dm.remove_group(group_id)
                except Exception:
                    pass
            return

        selected = dialog.selected_items()
        if not selected:
            if group_id:
                try:
                    dm = get_download_manager()
                    dm.remove_group(group_id)
                except Exception:
                    pass
            return

        try:
            target = handler.resolve_downloads(info, selected)
            dm = get_download_manager()
            if group_id:
                dm.finalize_resolving_group(group_id, target)
            else:
                dm.submit(target)
            self._poll()
            if target.skipped:
                self._show_skipped_warning(info.game_title, target.skipped)
        except Exception as e:
            QMessageBox.warning(self, _("Download Error"), str(e))

    def _show_skipped_warning(
        self, game_title: str, skipped: list[dict[str, str]],
    ) -> None:
        lines = []
        for item in skipped:
            lines.append(
                f"{item['name']}\n"
                f"  URL: {item['url']}\n"
                f"  {_('Reason')}: {item['reason']}"
            )
        detail = "\n\n".join(lines)
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle(_("Skipped Downloads"))
        msg.setText(
            ngettext(
                "{count} file for {title} could not be resolved and was skipped.",
                "{count} files for {title} could not be resolved and were skipped.",
                len(skipped),
            ).format(count=len(skipped), title=game_title)
        )
        msg.setDetailedText(detail)
        msg.exec()

    # -- Selection helpers --

    def _selected_group(self) -> Optional[dict]:
        idx = self._list_view.currentIndex()
        if not idx.isValid():
            return None
        return idx.data(GROUP_ROLE)

    def _update_button_states(self, *_args) -> None:
        group = self._selected_group()
        has_sel = group is not None
        status = group.get("status", "") if group else ""

        self._btn_move_up.setEnabled(has_sel)
        self._btn_move_down.setEnabled(has_sel)

        can_start = status in ("PAUSED", "CANCELLED", "FAILED", "PENDING")
        can_pause = status in ("DOWNLOADING", "PENDING")
        can_cancel = status not in ("COMPLETED", "CANCELLED", "")

        self._btn_enqueue.setEnabled(has_sel and can_start)
        self._btn_pause.setEnabled(has_sel and can_pause)
        self._btn_cancel.setEnabled(has_sel and can_cancel)

    @staticmethod
    def _is_shift_held() -> bool:
        from PySide6.QtCore import Qt as _Qt
        return bool(QGuiApplication.keyboardModifiers()
                    & _Qt.KeyboardModifier.ShiftModifier)

    # -- Button handlers --

    def _on_move_up(self) -> None:
        group = self._selected_group()
        if not group:
            return
        try:
            dm = get_download_manager()
            dm.move_group_up(group["id"])
            self._poll()
        except (RuntimeError, AttributeError):
            pass

    def _on_move_down(self) -> None:
        group = self._selected_group()
        if not group:
            return
        try:
            dm = get_download_manager()
            dm.move_group_down(group["id"])
            self._poll()
        except (RuntimeError, AttributeError):
            pass

    def _on_enqueue(self) -> None:
        try:
            dm = get_download_manager()
            if self._is_shift_held():
                dm.resume_all()
            else:
                group = self._selected_group()
                if group:
                    dm.resume_group(group["id"])
        except RuntimeError:
            pass
        self._poll()

    def _on_pause(self) -> None:
        try:
            dm = get_download_manager()
            if self._is_shift_held():
                dm.pause_all()
            else:
                group = self._selected_group()
                if group:
                    dm.pause_group(group["id"])
        except RuntimeError:
            pass
        self._poll()

    def _on_cancel(self) -> None:
        try:
            dm = get_download_manager()
            if self._is_shift_held():
                for group in dm.get_queue():
                    dm.cancel_group(group["id"])
            else:
                group = self._selected_group()
                if group:
                    dm.cancel_group(group["id"])
        except RuntimeError:
            pass
        self._poll()

    def _on_clear(self) -> None:
        try:
            dm = get_download_manager()
            if self._is_shift_held():
                for group in dm.get_queue():
                    dm.cancel_group(group["id"])
                dm.clear_completed()
            else:
                dm.clear_completed()
            self._poll()
        except RuntimeError:
            pass

    def _open_download_folder(self, group: dict) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        try:
            from luducat.core.archivist.volume import VolumeManager
            from luducat.core.config import get_default_archive_path
            archive_path = Path(
                self._config.get(
                    "downloads.archive_path",
                    str(get_default_archive_path()),
                )
            )
            org = self._config.get("downloads.folder_organization",
                                   "store-slug")
            vm = VolumeManager(archive_path, org)
            rel = vm.relative_path(
                group.get("store_name", ""),
                group.get("store_app_id", ""),
                group.get("game_title", ""),
                "_placeholder",
            )
            game_dir = (archive_path / Path(rel)).parent
            if not game_dir.exists():
                game_dir = archive_path
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(game_dir)))
        except Exception:
            logger.exception("Failed to open download folder")

    # -- Context menu --

    def _on_context_menu(self, pos) -> None:
        index = self._list_view.indexAt(pos)
        if not index.isValid():
            return
        group = index.data(GROUP_ROLE)
        if not group:
            return

        status = group.get("status", "")
        gid = group["id"]
        menu = QMenu(self)

        if status == "RESOLVING":
            pass
        elif status in ("PAUSED", "CANCELLED", "FAILED", "PENDING"):
            menu.addAction(_("Resume"), lambda: self._ctx_resume(gid))
        if status in ("DOWNLOADING", "PENDING"):
            menu.addAction(_("Pause"), lambda: self._ctx_pause(gid))
        if status not in ("COMPLETED", "CANCELLED", "RESOLVING"):
            menu.addAction(_("Cancel"), lambda: self._ctx_cancel(gid))
        if status == "COMPLETED":
            menu.addAction(
                _("Open folder"),
                lambda: self._open_download_folder(group))

        if menu.actions():
            menu.addSeparator()
        menu.addAction(_("Remove"), lambda: self._ctx_remove(gid))

        menu.exec_(self._list_view.viewport().mapToGlobal(pos))

    def _ctx_resume(self, group_id: str) -> None:
        try:
            get_download_manager().resume_group(group_id)
        except RuntimeError:
            pass

    def _ctx_pause(self, group_id: str) -> None:
        try:
            get_download_manager().pause_group(group_id)
        except RuntimeError:
            pass

    def _ctx_cancel(self, group_id: str) -> None:
        try:
            get_download_manager().cancel_group(group_id)
        except RuntimeError:
            pass

    def _ctx_remove(self, group_id: str) -> None:
        try:
            get_download_manager().remove_group(group_id)
            self._poll()
        except RuntimeError:
            pass
