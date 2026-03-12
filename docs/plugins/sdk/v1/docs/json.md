# SDK: JSON (`sdk.json`)

JSON serialization with optional orjson acceleration.

## Import

```python
from luducat.plugins.sdk.json import loads, dumps, load, dump, HAS_ORJSON
```

Or use as a drop-in replacement for the `json` module:

```python
from luducat.plugins.sdk import json

data = json.loads(text)
text = json.dumps(data, indent=2)
```

## Functions

### `loads(s, **kwargs) -> Any`

Deserialize a JSON string or bytes to a Python object:

```python
data = loads('{"title": "Portal 2"}')
data = loads(b'{"title": "Portal 2"}')  # bytes also accepted
```

Uses orjson if available, falls back to stdlib `json`.

### `dumps(obj, *, indent=None, ensure_ascii=False, default=None, sort_keys=False, **kwargs) -> str`

Serialize a Python object to a JSON string:

```python
text = dumps({"title": "Portal 2", "year": 2011})
text = dumps(data, indent=2)  # Pretty-printed
```

**Always returns `str`**, even when orjson is the backend (orjson natively
returns `bytes`, but the SDK decodes it automatically).

### `load(fp, **kwargs) -> Any`

Deserialize from a file object. Always uses stdlib `json`:

```python
with open("data.json") as f:
    data = load(f)
```

### `dump(obj, fp, **kwargs)`

Serialize to a file object. Always uses stdlib `json`:

```python
with open("data.json", "w") as f:
    dump(data, f, indent=2)
```

## Constants

### `HAS_ORJSON: bool`

`True` if orjson is installed, `False` otherwise. Useful for conditional
behavior, but most plugins shouldn't need to check this:

```python
if HAS_ORJSON:
    # orjson is available, loads/dumps will be faster
    pass
```

### `JSONDecodeError`

The stdlib `json.JSONDecodeError`, regardless of backend. Use for exception
handling:

```python
from luducat.plugins.sdk.json import loads, JSONDecodeError

try:
    data = loads(maybe_json)
except JSONDecodeError:
    data = {}
```

## Gotchas

- **`dumps()` always returns `str`.** Unlike raw orjson which returns `bytes`,
  the SDK wrapper decodes automatically. You never need to `.decode()`.
- **`load()`/`dump()` always use stdlib.** orjson doesn't support file objects
  natively, so file operations always use the standard library.
- **`ensure_ascii=False` by default.** Non-ASCII characters are preserved.
  This matches modern Python conventions.
