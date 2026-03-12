# SDK Overview

**SDK Version:** 0.1.0

## Architecture

The Plugin SDK lives at `luducat/plugins/sdk/` and provides a clean interface
between plugins and the core application. Third-party plugins import only from
the SDK and `luducat.plugins.base` -- never from `luducat.core.*` directly.

### Module Categories

**Self-contained modules** have their own implementation code and zero imports
from `luducat.core`:

| Module | Purpose |
|--------|---------|
| [`json`](json.md) | JSON serialization with optional orjson acceleration |
| [`datetime`](datetime.md) | UTC timestamps and release date parsing |
| [`text`](text.md) | Title normalization for cross-store deduplication |

**Shim modules** delegate to implementations injected via the registry at
startup:

| Module | Purpose |
|--------|---------|
| [`network`](network.md) | HTTP client with domain enforcement |
| [`config`](config.md) | Application config reading/writing |
| [`cookies`](cookies.md) | Browser cookie access |
| [`dialogs`](dialogs.md) | Widget status, login checking, data reset |

**Re-export modules** expose core constants and types:

| Module | Purpose |
|--------|---------|
| [`constants`](constants.md) | App version, user agent, badge colors |
| [`ui`](ui.md) | Status labels, form groups, dialogs, icon tinting |
| [`storage`](storage.md) | Path-confined filesystem access |

### Import Rules

```python
# SAFE -- any OSI license
from luducat.plugins.base import AbstractGameStore, Game, EnrichmentData
from luducat.plugins.sdk.network import PluginHttpClient
from luducat.plugins.sdk.json import loads, dumps

# FORBIDDEN for third-party plugins -- GPL boundary
from luducat.core.database import Session  # Import audit will block this
```

The import audit runs at plugin load time. It scans all `.py` files in the
plugin directory for:

1. **Telemetry imports** (always blocked, even for bundled plugins):
   `analytics`, `sentry_sdk`, `mixpanel`, `amplitude`, `posthog`, etc.

2. **Core imports** (blocked for third-party, warning for bundled):
   Any `from luducat.core` or `import luducat.core` statement.

### Registry Injection

Shim modules don't contain their own logic. Instead, the core application
registers implementations at startup via `_registry.py`:

```
Application startup
    -> register_config(get_data_dir, get_cache_dir, ...)
    -> register_network_manager(manager)
    -> register_cookies(get_browser_cookies)
    -> register_dialogs(...)
```

This means SDK functions only work after the application has initialized. If
you call them during module-level code (before the app starts), you'll get
`SdkNotInitializedError`. Always call SDK functions inside methods, not at
import time.

### GPL Boundary

The SDK is designed as a legal boundary:

- **`luducat.plugins.base`** -- Base classes, dataclasses, exceptions.
  Safe to import from any license.
- **`luducat.plugins.sdk.*`** -- Utility modules.
  Safe to import from any license.
- **`luducat.core.*`** -- Core application code.
  GPL derivative if imported directly.

See [Licensing](../../../licensing.md) for full details.

## Injected Properties

When the plugin manager loads your plugin, it injects several properties onto
your plugin instance:

| Property | Type | Available On | Description |
|----------|------|-------------|-------------|
| `self.http` | `PluginHttpClient` | All types | Rate-limited, domain-checked HTTP |
| `self.storage` | `PluginStorage` | All types | Path-confined file access |
| `self.main_db` | `MainDbAccessor` | Store only | Dict-based main DB access |
| `self._credential_manager` | `CredentialManager` | Store, Metadata | Keyring-backed credential storage |
| `self._settings` | `dict` | All types | Plugin settings from config |
| `self._local_data_consent` | `bool` | Store, Metadata | Privacy consent flag |

Use the helper methods rather than accessing private attributes directly:

```python
# Credentials
api_key = self.get_credential("api_key")
self.set_credential("api_key", new_key)

# Settings
value = self.get_setting("auto_enrich", default=True)

# Privacy
if self.has_local_data_consent():
    # Read local launcher data
    ...
```

## Module Map

```
luducat/plugins/sdk/
    __init__.py         SDK_VERSION = "0.1.0"
    _registry.py        Core -> SDK injection point (internal)
    app_finder.py       find_application, find_url_handler, AppSearchResult
    config.py           get_data_dir, get_cache_dir, get/set_config_value
    constants.py        APP_NAME, APP_VERSION, USER_AGENT, badge/tier colors
    cookies.py          get_browser_cookie_manager
    datetime.py         utc_now, utc_from_timestamp, parse/format_release_date
    dialogs.py          set_status_property, get_login_status, reset_plugin_data
    json.py             loads, dumps, load, dump, HAS_ORJSON
    network.py          PluginHttpClient, is_online
    proxy.py            get_proxy_url, build_proxy_headers, build_route_headers
    storage.py          PluginStorage, PluginStorageError
    text.py             normalize_title
    ui.py               create_status_label, create_form_group, show_*, open_url, load_tinted_icon
```
