# SDK: UI (`sdk.ui`)

Themed UI helpers for plugin settings dialogs.

## Import

```python
from luducat.plugins.sdk.ui import (
    create_status_label,
    create_form_group,
    show_confirmation,
    show_error,
    show_info,
    open_url,
    load_tinted_icon,
)
```

## Functions

### `create_status_label() -> QLabel`

Create a themed status label with `objectName="statusLabel"`. Used in plugin
settings dialogs to show connection/auth status:

```python
self.status_label = create_status_label()
layout.addWidget(self.status_label)
self.status_label.setText("Connected")
```

The label picks up QSS styling automatically through its object name.

### `create_form_group(title: str) -> Tuple[QGroupBox, QFormLayout]`

Create a settings group box with a form layout:

```python
group, form = create_form_group("Authentication")
form.addRow("API Key:", self.api_key_input)
form.addRow("Steam ID:", self.steam_id_input)
layout.addWidget(group)
```

### `show_confirmation(parent, title, message) -> bool`

Show a themed Yes/No confirmation dialog. Returns `True` if the user
clicks Yes:

```python
if show_confirmation(self, "Reset Data", "Delete all cached data?"):
    self.storage.delete("catalog.db")
```

### `show_error(parent, title, message)`

Show a themed error dialog:

```python
show_error(self, "Connection Failed", "Could not reach the API server.")
```

### `show_info(parent, title, message)`

Show a themed information dialog:

```python
show_info(self, "Sync Complete", f"Updated {count} games.")
```

### `open_url(url: str) -> None`

Open a URL in the user's preferred browser. Delegates to the browser manager
via registry, with fallback to `QDesktopServices.openUrl()`:

```python
open_url("https://store.example.com/game/42")
```

### `load_tinted_icon(svg_path, size=16, color=None) -> QIcon`

Load an SVG icon with palette-aware tinting. If `color` is `None`, the icon
is tinted to match the current theme's text color:

```python
# Relative to assets/icons/
icon = load_tinted_icon("refresh.svg", size=20)
button.setIcon(icon)

# Absolute path
icon = load_tinted_icon("/path/to/icon.svg")

# Custom color
from PySide6.QtGui import QColor
icon = load_tinted_icon("star.svg", color=QColor("#f0c040"))
```

## QSS Integration

All SDK UI helpers use object names and palette references that integrate with
luducat's theme system. Don't hardcode colors or font sizes:

```python
# GOOD -- uses themed status property
from luducat.plugins.sdk.dialogs import set_status_property
set_status_property(self.status_label, "success")

# BAD -- hardcoded color
self.status_label.setStyleSheet("color: green;")
```

## Gotchas

- **PySide6 import.** These functions use PySide6 widgets. They're only
  available when the GUI is running, not in headless/test environments.
- **Theme-aware.** All widgets created by these functions respect the active
  theme. Don't override their styling with hardcoded values.
