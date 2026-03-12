# SDK: Dialogs (`sdk.dialogs`)

Widget status helpers, login status checking, and plugin data reset.

## Import

```python
from luducat.plugins.sdk.dialogs import (
    set_status_property,
    get_browser_login_config_class,
    get_login_status,
    reset_plugin_data,
)
```

## Functions

### `set_status_property(widget, status, bold=False)`

Set a QSS-driven status property on a widget and force a style refresh.
The status string maps to theme-defined colors:

```python
set_status_property(self.status_label, "success")     # Green
set_status_property(self.status_label, "error")        # Red
set_status_property(self.status_label, "warning")      # Yellow/orange
set_status_property(self.status_label, "info")         # Default text
set_status_property(self.status_label, "success", bold=True)  # Bold green
```

Themes define colors for these status values via QSS property selectors.

### `get_browser_login_config_class() -> type | None`

Return the `BrowserLoginConfig` dataclass, if registered. Used internally
by plugin config dialogs that need browser login widgets.

### `get_login_status(domain, required_cookie) -> tuple`

Check login status for a cookie-based service. Returns a tuple of
`(is_logged_in: bool, browser_name: str | None)`:

```python
logged_in, message = get_login_status("gog.com", "gog-al")
if logged_in:
    self.status_label.setText(message)  # "Logged in"
```

### `reset_plugin_data(parent_widget, plugin_name, display_name, ...)`

Reset all data for a plugin using the shared reset path. This is a complex
function used by plugin config dialogs to implement "Reset Data" buttons.

Parameters:
- `parent_widget` -- parent widget for dialogs
- `plugin_name` -- plugin identifier
- `display_name` -- human-readable name for dialogs
- `plugin_types` -- list of plugin type strings
- `config` -- Config instance
- `status_label` -- QLabel to update with status
- `store_data_reset_signal` -- signal to emit on reset
- `get_game_service_fn` -- callable returning GameService
- `get_plugin_instance_fn` -- callable returning plugin instance
- `collect_image_urls_fn` -- optional callable for image URL collection

Most plugins won't call this directly -- it's used by the generic plugin
config dialog infrastructure.

## Gotchas

- **QSS status values.** The exact set of supported status strings depends on
  the theme. The standard ones are: `"success"`, `"error"`, `"warning"`,
  `"info"`. Custom themes may define additional values.
- **GUI-only.** These functions require the Qt event loop to be running.
