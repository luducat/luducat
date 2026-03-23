# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# engine.py

"""Declarative store engine — ruleset loader, backend dispatcher, field extraction.

Loads JSON rulesets that describe how to fetch game libraries from web stores.
Each ruleset defines auth, library fetch, detail fetch, pagination, and field
extraction rules. The engine dispatches to HTML or API backends based on the
ruleset's declared backend type.
"""

import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from luducat.core.json_compat import json

logger = logging.getLogger(__name__)

# Current ruleset schema version
SCHEMA_VERSION = 1

# Required top-level fields in a ruleset
_REQUIRED_FIELDS = {"schema_version", "store_name", "display_name", "library"}

# Valid auth types
_VALID_AUTH_TYPES = {"none", "browser_cookies", "api_key", "api_token", "bearer_redirect", "form_login"}

# Valid backend types
_VALID_BACKENDS = {"html", "api"}

# Valid pagination types
_VALID_PAGINATION_TYPES = {"page_param", "offset_param", "cursor", "next_link"}

# Valid transform functions
_VALID_TRANSFORMS = {"strip", "lowercase", "parse_date", "join_comma", "html_to_text"}


class RulesetError(Exception):
    """Raised when a ruleset is invalid or cannot be loaded."""


class Ruleset:
    """Parsed and validated store ruleset."""

    __slots__ = (
        "raw", "store_name", "display_name", "homepage",
        "brand_colors", "badge_label", "auth", "library",
        "detail", "launch", "rate_limit", "domains",
        "content_filter",
    )

    def __init__(self, data: dict, source_path: Optional[Path] = None):
        self.raw = data
        self.store_name = data["store_name"]
        self.display_name = data["display_name"]
        self.homepage = data.get("homepage", "")
        self.brand_colors = data.get("brand_colors", {})
        self.badge_label = data.get("badge_label", self.display_name.upper()[:3])
        self.auth = data.get("auth", {"type": "none"})
        self.library = data["library"]
        self.detail = data.get("detail")
        self.launch = data.get("launch", {})
        self.rate_limit = data.get("rate_limit", {"calls": 60, "window_seconds": 60})
        self.content_filter = data.get("content_filter", {})
        self.domains = self._extract_domains(data)

    def _extract_domains(self, data: dict) -> List[str]:
        """Auto-extract allowed domains from all URLs in the ruleset."""
        domains = set()
        urls = []

        # Collect all URL strings from the ruleset
        def _collect_urls(obj, depth=0):
            if depth > 10:
                return
            if isinstance(obj, str) and obj.startswith("http"):
                urls.append(obj)
            elif isinstance(obj, dict):
                for v in obj.values():
                    _collect_urls(v, depth + 1)
            elif isinstance(obj, list):
                for v in obj:
                    _collect_urls(v, depth + 1)

        _collect_urls(data)

        for url in urls:
            try:
                parsed = urlparse(url)
                if parsed.hostname:
                    domains.add(parsed.hostname)
            except Exception:
                pass

        return sorted(domains)


def load_rulesets(
    bundled_dir: Path,
    user_dir: Optional[Path] = None,
) -> List[Ruleset]:
    """Load and validate rulesets from bundled and user directories.

    Args:
        bundled_dir: Path to bundled rulesets (inside plugin dir)
        user_dir: Optional path to user-contributed rulesets

    Returns:
        List of validated Ruleset objects
    """
    rulesets = []

    for rulesets_dir in [bundled_dir, user_dir]:
        if not rulesets_dir or not rulesets_dir.exists():
            logger.debug("Rulesets dir not found: %s", rulesets_dir)
            continue

        logger.debug("Scanning rulesets in %s", rulesets_dir)
        for path in sorted(rulesets_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                _validate_ruleset(data, path)
                ruleset = Ruleset(data, source_path=path)
                rulesets.append(ruleset)
                logger.info(
                    "Loaded ruleset: %s (backend=%s, domains=%d, auth=%s) from %s",
                    ruleset.store_name,
                    ruleset.library.get("backend", "?"),
                    len(ruleset.domains),
                    ruleset.auth.get("type", "none"),
                    path.name,
                )
            except RulesetError as e:
                logger.error("Invalid ruleset %s: %s", path.name, e)
            except Exception as e:
                logger.error("Failed to load ruleset %s: %s", path.name, e)

    return rulesets


def _validate_ruleset(data: dict, path: Path) -> None:
    """Validate a ruleset dict against the schema."""
    # Check required fields
    missing = _REQUIRED_FIELDS - set(data.keys())
    if missing:
        raise RulesetError(f"missing required fields: {', '.join(sorted(missing))}")

    # Schema version
    if data.get("schema_version") != SCHEMA_VERSION:
        raise RulesetError(
            f"unsupported schema_version {data.get('schema_version')} "
            f"(expected {SCHEMA_VERSION})"
        )

    # Store name must be lowercase alphanumeric + underscore
    store_name = data["store_name"]
    if not re.match(r"^[a-z][a-z0-9_]*$", store_name):
        raise RulesetError(
            f"store_name '{store_name}' must be lowercase alphanumeric + underscore"
        )

    # Auth type validation
    auth = data.get("auth", {})
    auth_type = auth.get("type", "none")
    if auth_type not in _VALID_AUTH_TYPES:
        raise RulesetError(f"invalid auth type: {auth_type}")

    # Library backend validation
    library = data["library"]
    backend = library.get("backend")
    if backend not in _VALID_BACKENDS:
        raise RulesetError(f"invalid library backend: {backend}")

    # Detail backend validation (if present)
    detail = data.get("detail")
    if detail:
        detail_backend = detail.get("backend")
        if detail_backend not in _VALID_BACKENDS:
            raise RulesetError(f"invalid detail backend: {detail_backend}")


# ─── Field extraction utilities ───────────────────────────────────────

def extract_json_path(
    data: Any, path: str, array_filter: Optional[dict] = None,
) -> Any:
    """Extract a value from nested JSON using dot-notation path.

    Supports:
    - Simple paths: "title", "data.name"
    - Array iteration: "items[].name" (returns list)
    - Nested arrays: "data.items[].tags[].name"
    - Array filtering: filter items by sibling field value

    Args:
        data: JSON-parsed dict or list
        path: Dot-notation path string
        array_filter: Optional filter spec {"field": ..., "equals": ...}

    Returns:
        Extracted value, list of values, or None if path doesn't resolve
    """
    if data is None:
        return None
    if not path:
        return data  # Empty path = return root

    parts = path.split(".")
    return _resolve_path(data, parts, array_filter=array_filter)


def _resolve_path(
    data: Any, parts: List[str], array_filter: Optional[dict] = None,
) -> Any:
    """Recursively resolve a dot-path against data."""
    if not parts:
        return data

    part = parts[0]
    remaining = parts[1:]

    # Array iteration: "items[]"
    if part.endswith("[]"):
        key = part[:-2]
        if key:
            data = data.get(key) if isinstance(data, dict) else None
        if not isinstance(data, list):
            return None
        # Apply array filter if this is the terminal array (no remaining path
        # parts that would descend further into sub-arrays)
        if array_filter:
            data = _apply_array_filter(data, array_filter)
        results = []
        for item in data:
            val = _resolve_path(item, remaining)
            if isinstance(val, list):
                results.extend(val)
            elif val is not None:
                results.append(val)
        return results if results else None

    # Dict key
    if isinstance(data, dict):
        val = data.get(part)
        if val is None:
            return None
        return _resolve_path(val, remaining)

    return None


def _apply_array_filter(items: List[Any], filt: dict) -> List[Any]:
    """Filter array items by a sibling field value.

    Supports:
        {"field": "type", "equals": "cover"}
        {"field": "type", "not_in": ["cover", "thumbnail"]}
    """
    field = filt.get("field", "")
    if not field:
        return items

    if "equals" in filt:
        target = filt["equals"]
        return [
            item for item in items
            if isinstance(item, dict) and item.get(field) == target
        ]

    if "not_in" in filt:
        excluded = set(filt["not_in"])
        return [
            item for item in items
            if isinstance(item, dict) and item.get(field) not in excluded
        ]

    return items


def apply_transform(value: Any, transform: str) -> Any:
    """Apply a transform function to an extracted value.

    Args:
        value: The value to transform
        transform: Transform name ("strip", "lowercase", "parse_date", etc.)

    Returns:
        Transformed value
    """
    if value is None:
        return None

    if transform == "strip":
        return value.strip() if isinstance(value, str) else value

    if transform == "lowercase":
        return value.lower() if isinstance(value, str) else value

    if transform == "parse_date":
        # Try to normalize date strings to YYYY-MM-DD
        if isinstance(value, str):
            value = value.strip()
            # Already ISO format
            if re.match(r"^\d{4}-\d{2}-\d{2}", value):
                return value[:10]
            # US format: MM/DD/YYYY
            m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", value)
            if m:
                return f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
            # European: DD.MM.YYYY
            m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", value)
            if m:
                return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
        return value

    if transform == "join_comma":
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return value

    if transform == "join_html":
        if isinstance(value, list):
            return "<br>".join(str(v) for v in value)
        return value

    if transform == "html_to_text":
        if isinstance(value, str):
            # Strip HTML tags
            return re.sub(r"<[^>]+>", "", value).strip()
        return value

    logger.warning("Unknown transform: %s", transform)
    return value


def absolutize_html_urls(html: str, base_url: str) -> str:
    """Make relative URLs in HTML src/href attributes absolute.

    Handles both '/path' (root-relative) and 'path' (page-relative) forms.
    """
    base = base_url.rstrip("/")

    def _fix(m):
        attr = m.group(1)  # src or href
        quote = m.group(2)  # ' or "
        url = m.group(3)
        if url.startswith(("http://", "https://", "data:", "mailto:", "#")):
            return m.group(0)
        if url.startswith("/"):
            return f'{attr}={quote}{base}{url}{quote}'
        return f'{attr}={quote}{base}/{url}{quote}'

    return re.sub(r'(src|href)=(["\'])([^"\']*)\2', _fix, html)


def apply_field_spec(value: Any, spec: dict) -> Any:
    """Apply field specification (filter, first, prefix, regex, transform, wrap_array).

    Args:
        value: Raw extracted value
        spec: Field specification dict from ruleset

    Returns:
        Processed value
    """
    # Default value
    if value is None:
        value = spec.get("default")
    if value is None:
        return None

    # "first": take first element of a list result
    if spec.get("first") and isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None

    # Prefix (for relative URLs)
    prefix = spec.get("prefix", "")
    if prefix and isinstance(value, str):
        if not value.startswith(("http://", "https://")):
            value = prefix + value
    elif prefix and isinstance(value, list):
        value = [
            prefix + v if isinstance(v, str) and not v.startswith(("http://", "https://"))
            else v
            for v in value
        ]

    # Regex extraction (group 1)
    regex = spec.get("regex")
    if regex and isinstance(value, str):
        m = re.search(regex, value)
        if m and m.groups():
            value = m.group(1)
        elif m:
            value = m.group(0)

    # Transform
    transform = spec.get("transform")
    if transform:
        value = apply_transform(value, transform)

    # Wrap scalar in array
    if spec.get("wrap_array") and not isinstance(value, list):
        value = [value]

    return value
