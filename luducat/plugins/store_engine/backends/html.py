# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# html.py

"""HTML extraction backend — BeautifulSoup + CSS selectors.

Handles static HTML pages (server-rendered). Extracts fields using CSS
selectors, handles pagination, and injects browser cookies for auth.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from bs4 import BeautifulSoup, Tag

from ..engine import apply_field_spec

logger = logging.getLogger(__name__)


def extract_items(
    html: str,
    item_selector: str,
    fields: Dict[str, dict],
) -> List[Dict[str, Any]]:
    """Extract items from HTML using CSS selectors.

    Args:
        html: Raw HTML string
        item_selector: CSS selector for item containers
        fields: Field name -> extraction spec mapping

    Returns:
        List of dicts, one per item, with extracted field values
    """
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(item_selector)
    logger.debug("HTML extraction: selector '%s' matched %d elements (%d fields)",
                 item_selector, len(items), len(fields))
    results = []

    for item in items:
        row = {}
        for field_name, spec in fields.items():
            value = _extract_field(item, spec)
            value = apply_field_spec(value, spec)
            if value is not None:
                row[field_name] = value
        if row:
            results.append(row)

    return results


def _extract_field(element: Tag, spec: dict) -> Any:
    """Extract a single field value from an HTML element.

    Spec keys:
        selector: CSS selector relative to element (optional — uses element itself)
        text: Extract text content (default behavior)
        html: Extract inner HTML
        attr: Extract attribute value
        multiple: Return list of all matches (default: first match only)
    """
    selector = spec.get("selector")

    if spec.get("multiple"):
        targets = element.select(selector) if selector else [element]
        if not targets:
            return None
        values = []
        for t in targets:
            v = _get_value(t, spec)
            if v is not None:
                values.append(v)
        return values if values else None

    # Single value
    if selector:
        if spec.get("non_empty"):
            # Find first match with non-empty value
            for candidate in element.select(selector):
                v = _get_value(candidate, spec)
                if v:
                    return v
            return None
        target = element.select_one(selector)
    else:
        target = element

    if target is None:
        return None

    return _get_value(target, spec)


def _get_value(tag: Tag, spec: dict) -> Optional[str]:
    """Get a value from a BS4 tag based on the spec."""
    if spec.get("attr"):
        val = tag.get(spec["attr"])
        return str(val) if val is not None else None

    if spec.get("html"):
        return str(tag.decode_contents()).strip()

    # Default: text content
    return tag.get_text(strip=True) or None


def has_next_page(
    html: str,
    pagination: dict,
    current_page: int,
) -> bool:
    """Check if there's a next page based on pagination config.

    Args:
        html: Current page HTML
        pagination: Pagination config from ruleset
        current_page: Current page number

    Returns:
        True if there are more pages to fetch
    """
    pag_type = pagination.get("type", "page_param")

    if pag_type == "page_param":
        stop_when = pagination.get("stop_when", "no_items")
        if stop_when == "no_items":
            # Check if items were found on this page
            item_selector = pagination.get("item_selector")
            if item_selector:
                soup = BeautifulSoup(html, "html.parser")
                return len(soup.select(item_selector)) > 0
            return True  # Can't determine, assume more pages

    if pag_type == "next_link":
        selector = pagination.get("selector")
        if selector:
            soup = BeautifulSoup(html, "html.parser")
            return soup.select_one(selector) is not None

    return False


def get_next_page_url(html: str, pagination: dict) -> Optional[str]:
    """Extract next page URL from HTML for next_link pagination.

    Args:
        html: Current page HTML
        pagination: Pagination config

    Returns:
        Next page URL or None
    """
    if pagination.get("type") != "next_link":
        return None

    selector = pagination.get("selector")
    if not selector:
        return None

    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one(selector)
    if link is None:
        return None

    href = link.get("href")
    return str(href) if href else None
