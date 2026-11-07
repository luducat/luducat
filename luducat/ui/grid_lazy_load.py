# This file is part of the luducat Project.
# License: GPL-3.0-or-later. Contact: luducat-project@trinity2k.net
#
"""Viewport helpers shared by the IconMode grid views.

CoverView and ScreenshotView both lazy-load per-item data for what is
on screen. The visible-range probe lives here so both grids use the
same, tested logic.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractItemModel, QPoint
from PySide6.QtWidgets import QListView

# Last resort when neither visualRect() nor gridSize() report a size
_FALLBACK_ITEM_PX = 250


def visible_row_range(
    list_view: QListView, model: QAbstractItemModel,
) -> tuple[int, int] | None:
    """Return (first_row, last_row) of the items in the viewport.

    O(1): two indexAt() probes instead of a walk over the model. Returns
    None when the view is not laid out yet (empty model, zero-size
    viewport, or a model reset whose layout has not run); callers retry
    on a timer in that case.
    """
    row_count = model.rowCount()
    if row_count == 0:
        return None

    rect = list_view.viewport().rect()
    if rect.width() <= 0 or rect.height() <= 0:
        return None

    # Probe just inside the spacing: QPoint(0,0) sits in the inter-item
    # gap in IconMode and indexAt() reports invalid there.
    spacing = list_view.spacing()
    probe = QPoint(spacing + 1, spacing + 1)
    top_index = list_view.indexAt(rect.topLeft() + probe)
    if not top_index.isValid():
        return None

    bottom_index = list_view.indexAt(rect.bottomRight() - probe)
    if bottom_index.isValid():
        return top_index.row(), bottom_index.row()

    # Bottom-right landed in a gap (right margin of a partial column,
    # inter-row spacing) or past the last item. Estimate from the item
    # pitch instead of falling back to the whole model, which would
    # re-create the alphabetical full queue.
    item = list_view.visualRect(top_index)
    item_w, item_h = item.width(), item.height()
    if item_w <= 0 or item_h <= 0:
        grid = list_view.gridSize()
        item_w = grid.width() if grid.width() > 0 else _FALLBACK_ITEM_PX
        item_h = grid.height() if grid.height() > 0 else _FALLBACK_ITEM_PX
    cols = max(1, rect.width() // (item_w + spacing))
    rows = rect.height() // (item_h + spacing) + 2
    last_row = min(top_index.row() + rows * cols - 1, row_count - 1)
    return top_index.row(), last_row
