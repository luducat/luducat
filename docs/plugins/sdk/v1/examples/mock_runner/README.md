# GameVault Runner -- Demo Runner Plugin

A complete example runner plugin for the luducat Plugin SDK. Delegates game
launching to the fictional GameVault desktop application.

## What It Demonstrates

- All required `AbstractRunnerPlugin` methods
- Launcher detection across multiple paths
- Launch URI construction with `gamevault://` scheme
- Graceful handling of unsupported stores
- Settings-driven custom launcher path

## Files

| File | Purpose |
|------|---------|
| `plugin.json` | Runner capabilities, supported stores |
| `runner.py` | `GameVaultRunner` class with all methods |
| `__init__.py` | Package exports |

## See Also

- [Runner Plugin Guide](../../docs/runner-plugin.md)
- [Platform Plugin Guide](../../docs/platform-plugin.md) (the engine complement)
- [GameVault Store](../mock_store/) (the store this runner launches for)
