# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# sync_widget.py

"""Sync progress widget for luducat

Replaces the toolbar-embedded SyncProgressBar with a status-bar-area widget
that shows per-game progress, pause/resume, skip, and cancel controls.

Visible only during sync, in the main layout above the status bar.
Works in all view modes (list, cover, screenshot).

Layout when active:
    [Pause/Resume] [========progress_bar========] [Skip] [Cancel]

After sync finishes, shows a summary label that disappears on click.
"""

import logging
import time
from typing import Optional

from PySide6.QtCore import Qt, Signal, QTimer, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
)

from luducat.core.plugin_manager import PluginManager

logger = logging.getLogger(__name__)


class StripedProgressBar(QProgressBar):
    """Progress bar with animated diagonal stripes.

    Uses palette colors for theme integration:
    - Base: background
    - Mid: border
    - Highlight: chunk fill (accent color)
    - HighlightedText: text over chunk
    - Text: text over empty area

    In indeterminate mode (max==0), the entire bar is filled with
    animated stripes. In determinate mode, the chunk grows left-to-right
    with stripes inside.
    """

    STRIPE_WIDTH = 10
    BORDER_RADIUS = 3
    ANIMATION_INTERVAL_MS = 30  # ~33 FPS

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._stripe_offset = 0
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._advance_stripes)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._anim_timer.isActive():
            self._anim_timer.start(self.ANIMATION_INTERVAL_MS)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._anim_timer.stop()

    def _advance_stripes(self) -> None:
        self._stripe_offset = (self._stripe_offset + 1) % (self.STRIPE_WIDTH * 2)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        pal = self.palette()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        # --- Background + border ---
        bg_color = pal.color(QPalette.ColorRole.Button)
        border_color = pal.color(QPalette.ColorRole.Dark)
        painter.setPen(QPen(border_color, 1.0))
        painter.setBrush(bg_color)
        painter.drawRoundedRect(rect, self.BORDER_RADIUS, self.BORDER_RADIUS)

        # --- Chunk rect ---
        inset = rect.adjusted(1, 1, -1, -1)
        is_indeterminate = self.maximum() == 0 and self.minimum() == 0

        if is_indeterminate:
            chunk_rect = inset
        else:
            frac = 0.0
            rng = self.maximum() - self.minimum()
            if rng > 0:
                frac = (self.value() - self.minimum()) / rng
            chunk_w = inset.width() * frac
            if chunk_w < 1:
                # No chunk to draw, just paint text and return
                self._paint_text(painter, inset, None)
                painter.end()
                return
            chunk_rect = QRectF(inset.x(), inset.y(), chunk_w, inset.height())

        # --- Chunk fill + stripes ---
        highlight = pal.color(QPalette.ColorRole.Highlight)
        stripe_color = QColor(0, 0, 0, 30)

        # Clip to chunk within rounded inset (prevents stripe overflow)
        clip_path = QPainterPath()
        clip_path.addRoundedRect(inset, self.BORDER_RADIUS - 1, self.BORDER_RADIUS - 1)
        chunk_clip = QPainterPath()
        chunk_clip.addRect(chunk_rect)
        clip_path = clip_path.intersected(chunk_clip)
        painter.save()
        painter.setClipPath(clip_path)

        # Fill chunk with accent
        painter.fillRect(chunk_rect, highlight)

        # Draw diagonal stripes
        sw = self.STRIPE_WIDTH
        chunk_x = chunk_rect.x()
        chunk_bottom = chunk_rect.bottom()
        chunk_top = chunk_rect.top()
        ch = chunk_rect.height()
        total_span = int(chunk_rect.width()) + int(ch) + sw * 2

        for x in range(-int(ch) - sw * 2, total_span, sw * 2):
            stripe = QPainterPath()
            bx = chunk_x + x + self._stripe_offset
            stripe.moveTo(bx, chunk_bottom)
            stripe.lineTo(bx + sw, chunk_bottom)
            stripe.lineTo(bx + sw + ch, chunk_top)
            stripe.lineTo(bx + ch, chunk_top)
            stripe.closeSubpath()
            painter.fillPath(stripe, stripe_color)

        painter.restore()

        # --- Text ---
        self._paint_text(painter, inset, chunk_rect if not is_indeterminate else None)
        painter.end()

    def _paint_text(self, painter: QPainter, bar_rect: QRectF,
                    chunk_rect: Optional[QRectF]) -> None:
        """Draw centered format text with correct colors for chunk/empty areas."""
        if not self.isTextVisible():
            return
        text = self.format()
        if not text:
            return
        # Resolve Qt format placeholders (QProgressBar does this internally
        # in its own paintEvent, but we bypass that with custom painting)
        rng = self.maximum() - self.minimum()
        pct = int(100 * (self.value() - self.minimum()) / rng) if rng > 0 else 0
        text = text.replace("%p", str(pct)).replace(
            "%v", str(self.value())
        ).replace("%m", str(self.maximum()))

        pal = self.palette()
        painter.setFont(self.font())

        # Use clean integer rect for text centering (bar_rect has fractional
        # coords from anti-aliased border adjustments that round asymmetrically)
        text_rect = self.rect().adjusted(2, 2, -2, -2)

        if chunk_rect is None:
            # Indeterminate or no chunk — single color over highlight
            painter.setPen(pal.color(QPalette.ColorRole.HighlightedText))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)
        else:
            # Determinate — text spans chunk and empty areas
            # Draw text in highlight-text color, clipped to chunk
            painter.save()
            painter.setClipRect(chunk_rect)
            painter.setPen(pal.color(QPalette.ColorRole.HighlightedText))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)
            painter.restore()

            # Draw text in normal text color, clipped to empty area
            empty_rect = QRectF(
                chunk_rect.right(), bar_rect.y(),
                bar_rect.right() - chunk_rect.right(), bar_rect.height(),
            )
            if empty_rect.width() > 0:
                painter.save()
                painter.setClipRect(empty_rect)
                painter.setPen(pal.color(QPalette.ColorRole.Text))
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)
                painter.restore()


class SyncWidget(QWidget):
    """Sync progress widget shown in the status bar area during sync.

    Signals:
        pause_requested: User clicked pause/resume
        skip_requested: User clicked skip (advance to next plugin)
        cancel_requested: User clicked cancel
    """

    pause_requested = Signal()
    skip_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("syncWidget")
        self.setFixedHeight(36)

        self._queue = None
        self._is_paused = False

        # Track last progress for showing "(waiting)" during rate limits
        self._last_progress_plugin = ""
        self._last_progress_current = 0
        self._last_progress_total = 0
        self._phase_unit = ""  # "pages" for store fetch, "" for games

        # Countdown timer for rate limit display
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._tick_countdown)
        self._countdown_end_time = 0.0  # monotonic
        self._countdown_plugin = ""

        self._setup_ui()
        self.hide()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 12, 4)
        layout.setSpacing(8)

        # Pause/Resume button
        self._btn_pause = QPushButton(_("Pause"))
        self._btn_pause.setObjectName("syncPauseBtn")
        self._btn_pause.clicked.connect(self._on_pause_clicked)
        layout.addWidget(self._btn_pause)

        # Progress bar (stretches to fill) — custom StripedProgressBar
        # with animated stripes. Uses custom paintEvent that reads
        # directly from QPalette, bypassing platform style renderers
        # (KDE/Breeze ignores QSS ::chunk colors for stock QProgressBar).
        self._progress_bar = StripedProgressBar()
        self._progress_bar.setObjectName("syncProgressBar")
        self._progress_bar.setMinimum(0)
        self._progress_bar.setMaximum(0)  # indeterminate by default
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat(_("Starting sync..."))
        self._progress_bar.setFixedHeight(22)
        layout.addWidget(self._progress_bar, 1)  # stretch factor 1

        # Skip button
        self._btn_skip = QPushButton(_("Skip"))
        self._btn_skip.setObjectName("syncSkipBtn")
        self._btn_skip.setToolTip(_("Skip remaining work for the current plugin"))
        self._btn_skip.clicked.connect(self._on_skip_clicked)
        layout.addWidget(self._btn_skip)

        # Cancel button
        self._btn_cancel = QPushButton(_("Cancel"))
        self._btn_cancel.setObjectName("syncCancelBtn")
        self._btn_cancel.clicked.connect(self._on_cancel_clicked)
        layout.addWidget(self._btn_cancel)

        # Summary label (hidden initially, shown after sync finishes)
        self._summary_label = QLabel()
        self._summary_label.setObjectName("syncSummaryLabel")
        self._summary_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._summary_label.mousePressEvent = self._on_summary_clicked
        self._summary_label.hide()
        layout.addWidget(self._summary_label, 1)

    # --- Public API (called by MainWindow) ---

    def start(self, queue) -> None:
        """Show widget and begin tracking a sync queue.

        Args:
            queue: SyncJobQueue instance
        """
        self._queue = queue
        self._is_paused = False
        self._last_progress_plugin = ""
        self._last_progress_current = 0
        self._last_progress_total = 0
        self._phase_unit = ""
        self._countdown_timer.stop()
        self._countdown_end_time = 0.0
        self._countdown_plugin = ""

        # Show controls, hide summary
        self._btn_pause.setText(_("Pause"))
        self._btn_pause.show()
        self._btn_skip.show()
        self._btn_cancel.show()
        self._btn_cancel.setEnabled(True)
        self._btn_cancel.setText(_("Cancel"))
        self._progress_bar.show()
        self._summary_label.hide()

        # Indeterminate until first phase_started
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setFormat(_("Starting sync..."))
        self._progress_bar.setToolTip(_("Starting sync..."))

        self.show()

    def on_phase_started(self, plugin: str, desc: str, total: int) -> None:
        """A new sync phase has begun.

        Args:
            plugin: Plugin name (e.g. "steam", "igdb", "_system")
            desc: Human-readable description
            total: Number of items to process (0 = indeterminate)
        """
        name = PluginManager.get_store_display_name(plugin)
        # Track unit: store fetch phases use "pages", everything else is plain
        self._phase_unit = _(" pages") if "loading" in desc.lower() else ""
        if total == 0:
            # Indeterminate mode (use tooltip for status since text may be
            # hard to read on fully-filled striped bar)
            self._progress_bar.setRange(0, 0)
            phase_text = _("{name}: {desc}").format(name=name, desc=desc)
            self._progress_bar.setFormat(phase_text)
            self._progress_bar.setToolTip(phase_text)
        else:
            # Determinate mode
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(0)
            self._progress_bar.setFormat(
                _("{name}: {current}/{total}{unit}").format(
                    name=name, current=0, total=total, unit=self._phase_unit,
                )
            )
            self._progress_bar.setToolTip("")

    def on_phase_progress(self, plugin: str, current: int, total: int) -> None:
        """Progress within the current phase.

        Args:
            plugin: Plugin name
            current: Items completed so far
            total: Total items in this phase
        """
        name = PluginManager.get_store_display_name(plugin)
        # Sync maximum — other plugins' phase_started calls can overwrite it
        if self._progress_bar.maximum() != total:
            self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(current)
        self._progress_bar.setFormat(
            _("{name}: {current}/{total}{unit}").format(
                name=name, current=current, total=total, unit=self._phase_unit,
            )
        )
        self._last_progress_plugin = name
        self._last_progress_current = current
        self._last_progress_total = total

    def on_phase_finished(self, plugin: str) -> None:
        """A sync phase has completed.

        Args:
            plugin: Plugin name that finished
        """
        name = PluginManager.get_store_display_name(plugin)
        # Reset to indeterminate until next phase starts
        self._progress_bar.setRange(0, 0)
        done_text = _("{name}: done").format(name=name)
        self._progress_bar.setFormat(done_text)
        self._progress_bar.setToolTip(done_text)

    def on_rate_limit(self, message: str) -> None:
        """Show rate limit or wait status in the progress bar text.

        Only updates the progress bar for rate-limit/wait messages (containing
        "rate limited" or "remaining"). Regular fetch status messages (e.g.
        page progress) are handled by on_phase_progress and not overwritten here.
        Skipped while countdown timer is active (countdown takes priority).

        Args:
            message: Status message to display
        """
        # Countdown timer takes priority over generic rate limit text
        if self._countdown_timer.isActive():
            return
        is_rate_limit = message and (
            "rate limited" in message.lower()
            or "remaining" in message.lower()
        )
        if is_rate_limit and self._last_progress_total > 0:
            # Show progress with "(waiting)" in the bar, full message in tooltip
            self._progress_bar.setFormat(
                _("{name}: {current}/{total}{unit} (waiting)").format(
                    name=self._last_progress_plugin,
                    current=self._last_progress_current,
                    total=self._last_progress_total,
                    unit=self._phase_unit,
                )
            )
            self._progress_bar.setToolTip(message)
        elif is_rate_limit:
            self._progress_bar.setFormat(message)
            self._progress_bar.setToolTip(message)
        elif not message:
            # Cleared — restore last progress text if available
            if self._last_progress_total > 0:
                self._progress_bar.setFormat(
                    _("{name}: {current}/{total}{unit}").format(
                        name=self._last_progress_plugin,
                        current=self._last_progress_current,
                        total=self._last_progress_total,
                        unit=self._phase_unit,
                    )
                )
            self._progress_bar.setToolTip("")

    def on_rate_limit_countdown(self, plugin: str, wait_seconds: int) -> None:
        """Start or stop a live countdown in the progress bar.

        Args:
            plugin: Plugin name that hit rate limit
            wait_seconds: Seconds to count down (0 = stop countdown)
        """
        if wait_seconds > 0:
            self._countdown_plugin = PluginManager.get_store_display_name(plugin)
            self._countdown_end_time = time.monotonic() + wait_seconds
            self._tick_countdown()  # immediate first update
            if not self._countdown_timer.isActive():
                self._countdown_timer.start()
        else:
            self._countdown_timer.stop()
            self._countdown_end_time = 0.0
            self._countdown_plugin = ""
            # Restore normal progress text
            if self._last_progress_total > 0:
                self._progress_bar.setFormat(
                    _("{name}: {current}/{total}{unit}").format(
                        name=self._last_progress_plugin,
                        current=self._last_progress_current,
                        total=self._last_progress_total,
                        unit=self._phase_unit,
                    )
                )
                self._progress_bar.setToolTip("")

    def _tick_countdown(self) -> None:
        """Update progress bar text with remaining countdown time."""
        remaining = int(self._countdown_end_time - time.monotonic())
        if remaining <= 0:
            self._countdown_timer.stop()
            self._countdown_end_time = 0.0
            self._countdown_plugin = ""
            return

        minutes = remaining // 60
        seconds = remaining % 60
        if self._last_progress_total > 0:
            self._progress_bar.setFormat(
                _("{name}: {current}/{total} \u2014 retry in {m}:{ss}").format(
                    name=self._last_progress_plugin,
                    current=self._last_progress_current,
                    total=self._last_progress_total,
                    m=minutes,
                    ss=f"{seconds:02d}",
                )
            )
        else:
            self._progress_bar.setFormat(
                _("{name}: retry in {m}:{ss}").format(
                    name=self._countdown_plugin,
                    m=minutes,
                    ss=f"{seconds:02d}",
                )
            )

    def finish(self, stats: dict) -> None:
        """Sync completed successfully. Show summary, hide controls.

        Args:
            stats: Sync statistics dict from SyncWorker
        """
        summary = self._build_summary(stats)

        # Hide controls, show summary
        self._btn_pause.hide()
        self._btn_skip.hide()
        self._btn_cancel.hide()
        self._progress_bar.hide()

        self._summary_label.setText(summary)
        self._summary_label.setToolTip(self._build_tooltip(stats))
        self._summary_label.show()

        # Auto-hide after 10 seconds
        QTimer.singleShot(10000, self._auto_hide_summary)

    def on_cancelled(self, stats: dict) -> None:
        """Sync was cancelled. Hide widget.

        Args:
            stats: Partial sync statistics
        """
        self._queue = None
        self.hide()

    def shutdown(self) -> None:
        """Show shutdown state — hides controls, shows indeterminate bar."""
        self._btn_pause.hide()
        self._btn_skip.hide()
        self._btn_cancel.hide()
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setFormat(_("Shutting down..."))
        self._progress_bar.setToolTip("")
        self._progress_bar.show()
        self.show()

    # --- Private ---

    def _on_pause_clicked(self) -> None:
        if self._is_paused:
            self._is_paused = False
            self._btn_pause.setText(_("Pause"))
            if self._queue:
                self._queue.resume()
        else:
            self._is_paused = True
            self._btn_pause.setText(_("Resume"))
            if self._queue:
                self._queue.pause()
        self.pause_requested.emit()

    def _on_skip_clicked(self) -> None:
        self.skip_requested.emit()

    def _on_cancel_clicked(self) -> None:
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.setText(_("Cancelling..."))
        self._progress_bar.setFormat(_("Cancelling..."))
        self.cancel_requested.emit()

    def _on_summary_clicked(self, event) -> None:
        """Dismiss summary on click."""
        self._queue = None
        self.hide()

    def _auto_hide_summary(self) -> None:
        """Auto-hide summary label after timeout (only if still showing summary)."""
        if self._summary_label.isVisible() and not self._progress_bar.isVisible():
            self._queue = None
            self.hide()

    def _build_summary(self, stats: dict) -> str:
        """Build a one-line summary string from sync stats."""
        parts = []
        total_added = 0
        total_enriched = 0

        for store_name, store_stats in stats.items():
            if store_name.startswith("_"):
                continue
            if not isinstance(store_stats, dict):
                continue
            total_added += store_stats.get("games_added", 0)

        meta_stats = stats.get("_metadata", {})
        for plugin_stats in meta_stats.values():
            total_enriched += plugin_stats.get("enriched", 0)

        if total_added:
            parts.append(
                ngettext("+{n} game", "+{n} games", total_added).format(n=total_added)
            )
        if total_enriched:
            parts.append(
                _("{n} enriched").format(n=total_enriched)
            )

        errors = stats.get("errors", [])
        if errors:
            parts.append(
                ngettext("{n} error", "{n} errors", len(errors)).format(n=len(errors))
            )

        if parts:
            return _("Sync complete: {details}").format(details=", ".join(parts))
        return _("Sync complete: nothing to update")

    def _build_tooltip(self, stats: dict) -> str:
        """Build a detailed tooltip with per-store breakdown."""
        lines = []

        for store_name, store_stats in stats.items():
            if store_name.startswith("_"):
                continue
            if not isinstance(store_stats, dict):
                continue
            added = store_stats.get("games_added", 0)
            updated = store_stats.get("games_updated", 0)
            found = store_stats.get("games_found", 0)
            if added or updated or found:
                detail = []
                if found:
                    detail.append(
                        ngettext("{n} found", "{n} found", found).format(n=found)
                    )
                if added:
                    detail.append(
                        ngettext("{n} added", "{n} added", added).format(n=added)
                    )
                if updated:
                    detail.append(
                        ngettext("{n} updated", "{n} updated", updated).format(
                            n=updated
                        )
                    )
                lines.append(
                    _("{name}: {details}").format(
                        name=PluginManager.get_store_display_name(store_name),
                        details=", ".join(detail),
                    )
                )

        meta_stats = stats.get("_metadata", {})
        for plugin_name, plugin_stats in meta_stats.items():
            enriched = plugin_stats.get("enriched", 0)
            if enriched:
                lines.append(
                    _("{name}: {n} enriched").format(
                        name=PluginManager.get_store_display_name(plugin_name), n=enriched,
                    )
                )

        errors = stats.get("errors", [])
        if errors:
            lines.append(
                "\n" + ngettext(
                    "Errors ({n}):", "Errors ({n}):", len(errors)
                ).format(n=len(errors))
            )
            for err in errors[:5]:
                lines.append(f"  {err.get('job', '?')}: {err.get('error', '?')}")

        return "\n".join(lines) if lines else _("No changes")
