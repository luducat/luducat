# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# style_helpers.py

"""Style helpers for QSS property-driven theming.

Utility functions to set dynamic QSS properties on widgets,
replacing hardcoded setStyleSheet calls with the 3-layer
theme system.
"""


def set_status_property(widget, status: str, bold: bool = False):
    """Set status property on widget and refresh QSS styling.

    Uses dynamic properties so QSS selectors like
    ``QLabel[status="success"]`` can style the widget.

    Args:
        widget: Any QWidget
        status: Status value ("success", "error", "warning", or "")
        bold: If True, also sets fontWeight="bold" property
    """
    widget.setProperty("status", status)
    if bold:
        widget.setProperty("fontWeight", "bold")
    elif widget.property("fontWeight"):
        widget.setProperty("fontWeight", "")
    widget.style().unpolish(widget)
    widget.style().polish(widget)
