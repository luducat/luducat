# SDK: Config (`sdk.config`)

Application configuration access.

## Import

```python
from luducat.plugins.sdk.config import (
    get_data_dir,
    get_cache_dir,
    get_config_value,
    set_config_value,
)
```

## Functions

### `get_data_dir() -> Path`

Return the application's data directory:
- Linux: `~/.local/share/luducat/`
- Windows: `%APPDATA%/luducat/`
- macOS: `~/Library/Application Support/luducat/`

```python
data_dir = get_data_dir()
# Path('/home/user/.local/share/luducat')
```

### `get_cache_dir() -> Path`

Return the application's cache directory:
- Linux: `~/.cache/luducat/`
- Windows: `%LOCALAPPDATA%/luducat/cache/`
- macOS: `~/Library/Caches/luducat/`

```python
cache_dir = get_cache_dir()
# Path('/home/user/.cache/luducat')
```

### `get_config_value(key, default=None) -> Any`

Read a value from the global config using dotted key notation:

```python
browser = get_config_value("privacy.preferred_browser", default="auto")
zoom = get_config_value("appearance.zoom_level", default=100)
```

### `set_config_value(key, value)`

Write a value to the global config:

```python
set_config_value("plugins.my_store.last_sync", "2025-01-15")
```

## Gotchas

- **Prefer `self.get_setting()` for plugin settings.** The `get_config_value`
  function accesses the global config. Plugin-specific settings are injected
  via `self._settings` and accessed with `self.get_setting("key")`.
- **SDK not initialized error.** These functions require the registry to be
  initialized. Don't call them at module level or in `__init__`. Use them
  inside methods.
