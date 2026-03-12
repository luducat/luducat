# GamePedia -- Demo Metadata Plugin

A complete example metadata provider for the luducat Plugin SDK. Uses
hardcoded enrichment data -- no real API calls.

## What It Demonstrates

- All required `AbstractMetadataProvider` methods
- Store ID cross-reference lookup
- Title-based fuzzy search with confidence scoring
- `EnrichmentData` construction with genres, tags, franchise, themes
- `MetadataSearchResult` with ranking
- Cached enrichment pattern
- `provides_fields` priority declarations

## Files

| File | Purpose |
|------|---------|
| `plugin.json` | Plugin metadata with provides_fields priorities |
| `provider.py` | `GamePediaProvider` class with all methods |
| `__init__.py` | Package exports |

## See Also

- [Metadata Plugin Guide](../../docs/metadata-plugin.md)
- [plugin.json Reference](../../docs/plugin-json.md)
