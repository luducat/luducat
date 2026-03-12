# GameVault -- Demo Store Plugin

A complete example store plugin for the luducat Plugin SDK. Uses a fictional
game store with hardcoded data -- no real API calls are made.

## What It Demonstrates

- All required `AbstractGameStore` methods
- API key authentication via credential helpers
- `Game` dataclass construction with varied metadata richness
- Status callbacks and cancel checking during sync
- Config actions (dynamic settings dialog buttons)
- Lifecycle hooks (`on_enable`, `on_disable`, `close`)
- Store page URL generation
- Metadata bulk lookup

## Usage

Copy this directory to your luducat plugins folder:

```bash
cp -r docs/plugins/sdk/v1/examples/mock_store ~/.local/share/luducat/plugins/gamevault
```

Then enable "GameVault" in Settings > Plugins and sync.

## Files

| File | Purpose |
|------|---------|
| `plugin.json` | Plugin metadata, capabilities, settings schema |
| `store.py` | `GameVaultStore` class with all methods |
| `__init__.py` | Package exports |

## See Also

- [Store Plugin Guide](../../docs/store-plugin.md)
- [plugin.json Reference](../../docs/plugin-json.md)
- [Authentication Patterns](../../docs/authentication.md)
