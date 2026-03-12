# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# json.py

"""Drop-in JSON compatibility layer — orjson with stdlib fallback.

Self-contained: zero imports from ``luducat.core``.  Duplicated from
``core/json_compat.py`` so third-party plugins never touch GPL code.

Usage in plugins::

    from luducat.plugins.sdk.json import json
    data = json.loads(payload)
    text = json.dumps(data, indent=2)

Or import individual functions::

    from luducat.plugins.sdk.json import loads, dumps, JSONDecodeError
"""

import sys
import json as _stdlib_json
from typing import Any

try:
    import orjson as _orjson
except ImportError:
    _orjson = None  # type: ignore[assignment]

# Re-export JSONDecodeError — always stdlib (orjson's is a subclass anyway)
JSONDecodeError = _stdlib_json.JSONDecodeError

# Flag for introspection / logging
HAS_ORJSON: bool = _orjson is not None


# ── Core API ─────────────────────────────────────────────────────────


def loads(s, **kwargs) -> Any:
    """Deserialize JSON string or bytes to Python object."""
    if _orjson is not None and not kwargs:
        return _orjson.loads(s)
    return _stdlib_json.loads(s, **kwargs)


def dumps(obj, *, indent=None, ensure_ascii=False, default=None,
          sort_keys=False, **kwargs) -> str:
    """Serialize Python object to JSON string.

    Always returns str (not bytes) for stdlib compatibility.
    Maps common stdlib kwargs to orjson options where possible.
    """
    if _orjson is not None and not kwargs:
        option = 0
        if indent:
            option |= _orjson.OPT_INDENT_2
        if sort_keys:
            option |= _orjson.OPT_SORT_KEYS
        return _orjson.dumps(obj, default=default, option=option or None).decode("utf-8")

    return _stdlib_json.dumps(
        obj, indent=indent, ensure_ascii=ensure_ascii,
        default=default, sort_keys=sort_keys, **kwargs,
    )


def load(fp, **kwargs) -> Any:
    """Deserialize JSON from file object — always stdlib."""
    return _stdlib_json.load(fp, **kwargs)


def dump(obj, fp, *, indent=None, ensure_ascii=False, default=None,
         sort_keys=False, **kwargs) -> None:
    """Serialize Python object to JSON file — always stdlib."""
    _stdlib_json.dump(
        obj, fp, indent=indent, ensure_ascii=ensure_ascii,
        default=default, sort_keys=sort_keys, **kwargs,
    )


# Self-reference so ``from luducat.plugins.sdk.json import json`` provides
# a drop-in namespace:  json.loads(), json.dumps(), json.JSONDecodeError, etc.
json = sys.modules[__name__]
