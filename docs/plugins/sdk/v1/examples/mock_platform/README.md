# PixelEngine -- Demo Platform Plugin

A complete example platform provider for the luducat Plugin SDK. Fictional
retro game engine following the DOSBox/ScummVM pattern.

## What It Demonstrates

- All required `AbstractPlatformProvider` methods
- Platform detection across multiple paths (system, snap, custom)
- Game compatibility via tag and metadata inspection
- Launch configuration with arguments and environment
- Global and per-game settings schemas
- Settings-driven behavior (fullscreen, scale factor)

## Files

| File | Purpose |
|------|---------|
| `plugin.json` | Platform capabilities, game types, settings |
| `platform.py` | `PixelEnginePlatform` class with all methods |
| `__init__.py` | Package exports |

## See Also

- [Platform Plugin Guide](../../docs/platform-plugin.md)
- [Runner Plugin Guide](../../docs/runner-plugin.md) (the launch complement)
