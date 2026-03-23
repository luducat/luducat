# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# api.py

"""API extraction backend — JSON path extraction for REST APIs.

Handles stores with REST/JSON APIs. Extracts fields using dot-notation
paths into JSON responses. Supports bearer tokens, cookie auth, and
paginated responses.
"""

import logging
from typing import Any, Dict, List, Optional

from ..engine import apply_field_spec, extract_json_path

logger = logging.getLogger(__name__)


def extract_items(
    data: Any,
    items_path: str,
    fields: Dict[str, dict],
) -> List[Dict[str, Any]]:
    """Extract items from a JSON API response.

    Args:
        data: Parsed JSON response
        items_path: Dot-path to the items array (e.g., "products")
        fields: Field name -> extraction spec mapping

    Returns:
        List of dicts, one per item, with extracted field values
    """
    items = extract_json_path(data, items_path)
    if isinstance(items, dict):
        # Dict-keyed response (e.g. {slug: {product data}}) — iterate values
        items = list(items.values())
    elif not isinstance(items, list):
        if items is not None:
            # Single item, wrap
            items = [items]
        else:
            logger.debug("No items at path '%s'", items_path)
            return []

    logger.debug("Extracting %d items from '%s' (%d fields)",
                 len(items), items_path, len(fields))
    results = []
    for item in items:
        row = {}
        for field_name, spec in fields.items():
            path = spec.get("path", "")
            filt = spec.get("filter")
            value = extract_json_path(item, path, array_filter=filt)
            value = apply_field_spec(value, spec)
            if value is not None:
                row[field_name] = value
        if row:
            results.append(row)

    return results


def extract_detail(
    data: Any,
    fields: Dict[str, dict],
) -> Dict[str, Any]:
    """Extract detail fields from a single-item JSON response.

    Unlike extract_items, this operates on the response root (or a
    single object), not an array of items.

    Args:
        data: Parsed JSON response
        fields: Field name -> extraction spec mapping

    Returns:
        Dict of extracted field values
    """
    result = {}
    for field_name, spec in fields.items():
        path = spec.get("path", "")
        filt = spec.get("filter")
        value = extract_json_path(data, path, array_filter=filt)
        value = apply_field_spec(value, spec)
        if value is not None:
            result[field_name] = value
    logger.debug("Detail extraction: %d/%d fields populated", len(result), len(fields))
    return result


def get_total_pages(data: Any, pagination: dict) -> Optional[int]:
    """Extract total page count from API response.

    Args:
        data: Parsed JSON response
        pagination: Pagination config from ruleset

    Returns:
        Total pages or None if not determinable
    """
    total_path = pagination.get("total_path")
    if total_path:
        val = extract_json_path(data, total_path)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
    return None


def get_cursor(data: Any, pagination: dict) -> Optional[str]:
    """Extract cursor value for cursor-based pagination.

    Args:
        data: Parsed JSON response
        pagination: Pagination config

    Returns:
        Cursor string for next request or None
    """
    cursor_path = pagination.get("cursor_path")
    if cursor_path:
        val = extract_json_path(data, cursor_path)
        if val is not None:
            return str(val)
    return None


def has_more_items(
    data: Any,
    items_path: str,
    pagination: dict,
    current_page: int,
) -> bool:
    """Check if there are more pages of results.

    Args:
        data: Parsed JSON response
        items_path: Dot-path to items array
        pagination: Pagination config
        current_page: Current page number (1-indexed)

    Returns:
        True if more pages exist
    """
    pag_type = pagination.get("type", "page_param")

    if pag_type == "page_param":
        total = get_total_pages(data, pagination)
        if total is not None:
            return current_page < total

        # Fallback: check if items were returned
        stop_when = pagination.get("stop_when", "no_items")
        if stop_when == "no_items":
            items = extract_json_path(data, items_path)
            return isinstance(items, list) and len(items) > 0

    elif pag_type == "cursor":
        return get_cursor(data, pagination) is not None

    elif pag_type == "offset_param":
        total_path = pagination.get("total_path")
        if total_path:
            total = extract_json_path(data, total_path)
            if total is not None:
                limit = pagination.get("limit", 50)
                offset = (current_page - 1) * limit
                return offset + limit < int(total)

    return False
