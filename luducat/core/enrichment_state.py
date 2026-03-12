# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# enrichment_state.py

"""Enrichment state helpers for metadata_json dicts.

The enrichment pipeline tracks three pieces of state inside each
StoreGame.metadata_json["_sources"] dict:

  _sources[field_name] = provider_name   # who provided this field
  _sources["_attempted_by"] = [...]      # plugins that tried but found no match
  _sources["_enriched_via"] = plugin     # cross-store dedup marker

These helpers replace all raw dict access to these keys.
"""

SOURCES_KEY = "_sources"
ATTEMPTED_KEY = "_attempted_by"
ENRICHED_VIA_KEY = "_enriched_via"


# --- Queries ---


def get_sources(metadata: dict) -> dict:
    """Get the _sources dict, creating it if absent."""
    return metadata.setdefault(SOURCES_KEY, {})


def get_field_source(metadata: dict, field_name: str) -> str:
    """Return the provider name that supplied a field, or empty string."""
    return get_sources(metadata).get(field_name, "")


def is_enriched_by(metadata: dict, plugin_name: str) -> bool:
    """True if any field was supplied by this plugin.

    Only checks actual field sources — excludes internal keys
    (_attempted_by, _enriched_via).
    """
    sources = get_sources(metadata)
    return any(
        v == plugin_name
        for k, v in sources.items()
        if k not in (ATTEMPTED_KEY, ENRICHED_VIA_KEY) and isinstance(v, str)
    )


def is_attempted_by(metadata: dict, plugin_name: str) -> bool:
    """True if plugin tried but found no match."""
    return plugin_name in get_sources(metadata).get(ATTEMPTED_KEY, [])


def is_enriched_via_sibling(metadata: dict, plugin_name: str) -> bool:
    """True if a cross-store representative was enriched by this plugin."""
    return get_sources(metadata).get(ENRICHED_VIA_KEY) == plugin_name


# --- Mutations ---


def mark_field_source(metadata: dict, field_name: str, provider: str) -> None:
    """Record which provider supplied a field."""
    get_sources(metadata)[field_name] = provider


def mark_attempted(metadata: dict, plugin_name: str) -> bool:
    """Record that a plugin tried but found no match. Returns True if changed."""
    sources = get_sources(metadata)
    attempted = sources.setdefault(ATTEMPTED_KEY, [])
    if plugin_name not in attempted:
        attempted.append(plugin_name)
        return True
    return False


def mark_enriched_via_sibling(metadata: dict, plugin_name: str) -> None:
    """Mark this game as covered by cross-store dedup."""
    get_sources(metadata)[ENRICHED_VIA_KEY] = plugin_name


# Fields whose source is tracked in _sources and whose metadata_json key
# may differ from the field name used in _sources.
# After field normalization, most identity mappings removed — only genuine
# concept differences remain. Backward compat: also check old storage names.
_FIELD_TO_KEY = {
    "age_rating": "age_ratings",  # Singular query vs plural storage
    "rating": "user_rating",      # Generic rating vs IGDB user_rating
    # Backward compat: old storage names still exist in pre-normalization metadata_json
    "cover": "cover_url",         # Will be identity after re-sync
    "hero": "background_url",     # Will be identity after re-sync
    "links": "websites",          # Will be identity after re-sync
}

# Store plugin names — these fields should NOT be cleared on rescan
# because they were provided by the store's own metadata fetch.
# Populated dynamically from PluginManager on first call, then cached.
_store_plugins_cache: set | None = None


def _get_store_plugins() -> set:
    global _store_plugins_cache
    if _store_plugins_cache is not None:
        return _store_plugins_cache
    try:
        from .plugin_manager import PluginManager
        names = set(PluginManager.get_store_plugin_names())
        if names:
            _store_plugins_cache = names
            return names
    except Exception:
        pass
    return set()


def clear_enrichment(metadata: dict, *, force: bool = False) -> None:
    """Clear all enrichment state and enrichment-provided field values
    for a full rescan.

    Uses _sources to identify which fields were set by metadata plugins
    (non-store sources) and clears those field values so that the
    enrichment pipeline can re-apply them using current priority order.

    Args:
        metadata: The metadata_json dict to clear.
        force: If True, clear ALL tracked fields including store-provided
               ones. Used by force rescan to start completely from scratch.
    """
    sources = metadata.get(SOURCES_KEY, {})

    # Clear field values that were set by enrichment plugins
    for field_name, provider in list(sources.items()):
        if field_name in (ATTEMPTED_KEY, ENRICHED_VIA_KEY):
            continue
        if not isinstance(provider, str):
            continue
        if not force and provider in _get_store_plugins():
            continue
        # This field came from a metadata plugin — clear the actual value
        # Check both canonical name and old storage name for backward compat
        metadata_key = _FIELD_TO_KEY.get(field_name, field_name)
        metadata.pop(metadata_key, None)
        # Also clear the canonical name if _FIELD_TO_KEY gave us the old name
        if metadata_key != field_name:
            metadata.pop(field_name, None)
        # Also clear associated secondary keys
        if field_name == "hero":
            metadata.pop("background_provider", None)
            metadata.pop("background_url", None)  # Old storage name
        elif field_name == "cover":
            metadata.pop("cover_url", None)  # Old storage name
        elif field_name == "links":
            metadata.pop("websites", None)  # Old storage name
        elif field_name == "rating":
            metadata.pop("user_rating_count", None)

    metadata.pop(SOURCES_KEY, None)


# --- Debugging ---


def get_enrichment_summary(metadata: dict) -> dict:
    """Return a summary of enrichment state for debugging."""
    sources = metadata.get(SOURCES_KEY, {})
    field_sources = {k: v for k, v in sources.items()
                     if k not in (ATTEMPTED_KEY, ENRICHED_VIA_KEY)}
    return {
        "field_sources": field_sources,
        "attempted_by": sources.get(ATTEMPTED_KEY, []),
        "enriched_via": sources.get(ENRICHED_VIA_KEY),
        "provider_count": len(set(field_sources.values())),
    }
