# SDK: Storage (`sdk.storage`)

Path-confined filesystem access for plugins.

## Import

```python
from luducat.plugins.sdk.storage import PluginStorage, PluginStorageError
```

## `PluginStorage`

Provides safe file operations within a plugin's three designated directories.
All paths are validated to prevent traversal attacks (no `../` escaping).

You don't instantiate `PluginStorage` yourself. It's injected by the plugin
manager and available as `self.storage`:

```python
class MyStore(AbstractGameStore):
    def save_catalog(self, data):
        self.storage.write_text("catalog.json", json.dumps(data))
```

### Base Directories

Every plugin gets three base directories:

| Base | Path | Purpose |
|------|------|---------|
| `"config"` | `~/.config/luducat/plugins/{name}/` | User configuration |
| `"cache"` | `~/.cache/luducat/plugins/{name}/` | Temporary/regenerable data |
| `"data"` | `~/.local/share/luducat/plugins-data/{name}/` | Persistent data (databases, state) |

The default base for all operations is `"data"`.

### Reading Files

#### `read_file(relative_path, base="data") -> bytes`

```python
data = self.storage.read_file("catalog.json")
```

#### `read_text(relative_path, base="data", encoding="utf-8") -> str`

```python
text = self.storage.read_text("state.json")
config = self.storage.read_text("settings.ini", base="config")
```

### Writing Files

#### `write_file(relative_path, content: bytes, base="data")`

```python
self.storage.write_file("image.png", image_bytes, base="cache")
```

Parent directories are created automatically.

#### `write_text(relative_path, content: str, base="data", encoding="utf-8")`

```python
self.storage.write_text("state.json", json.dumps(state))
```

### Directory Operations

#### `list_dir(relative_path="", base="data") -> List[str]`

List filenames in a directory:

```python
files = self.storage.list_dir("exports")
# ["export_2024.csv", "export_2025.csv"]
```

#### `ensure_dir(relative_path, base="data") -> Path`

Create a directory (and parents). Returns the absolute path:

```python
export_dir = self.storage.ensure_dir("exports/2025")
```

### Path Operations

#### `exists(relative_path, base="data") -> bool`

```python
if self.storage.exists("catalog.json"):
    data = self.storage.read_text("catalog.json")
```

#### `delete(relative_path, base="data") -> bool`

Delete a file. Returns `True` if deleted, `False` if not found. Does NOT
delete directories.

```python
self.storage.delete("old_cache.json", base="cache")
```

#### `get_path(relative_path, base="data") -> Path`

Resolve a relative path to a validated absolute path. Useful when you need the
actual filesystem path (e.g., for SQLite connections):

```python
db_path = self.storage.get_path("catalog.db")
engine = create_engine(f"sqlite:///{db_path}")
```

#### `get_db_path(db_name="plugin.db") -> Path`

Shortcut for database paths. Returns a validated path in the data directory:

```python
db_path = self.storage.get_db_path("catalog.db")
```

### Storage Usage

#### `get_storage_usage() -> Dict[str, int]`

Get disk usage per base directory in bytes:

```python
usage = self.storage.get_storage_usage()
# {"config": 1024, "cache": 5242880, "data": 10485760}
```

## `PluginStorageError`

Raised on path traversal attempts or other storage violations:

```python
try:
    self.storage.read_file("../../etc/passwd")  # Blocked!
except PluginStorageError as e:
    logger.error(f"Storage violation: {e}")
```

## Gotchas

- **Path traversal is blocked.** Any path containing `..` that escapes the
  base directory raises `PluginStorageError`.
- **`self.storage` is `None` before injection.** Don't access it in
  `__init__`. Use it in lifecycle methods and API calls.
- **`delete()` only deletes files**, not directories. This is intentional
  to prevent accidental recursive deletion.
- **Use `get_db_path()` for databases.** It validates the path and ensures the
  data directory exists. Don't construct database paths manually.
