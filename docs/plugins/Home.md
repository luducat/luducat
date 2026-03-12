# luducat Plugin SDK

**SDK Version: 0.1.0**

Build plugins that extend luducat with new game stores, metadata sources,
game engines, and launch integrations. Plugins run inside a security sandbox
with domain enforcement, path confinement, and integrity verification.

The SDK is designed as a GPL boundary: plugins can use any OSI-approved
license (MIT, Apache, BSD, etc.) as long as they import only from the SDK
and base classes.

---

## Quick Links

| | |
|---|---|
| **[Quickstart](quickstart.md)** | Build your first plugin in 10 minutes |
| **[Plugin Generator](generator/generate_plugin.py)** | Generate a complete project scaffold |
| **[FAQ](FAQ.md)** | Common questions and answers |

---

## Documentation

### Getting Started

- [Quickstart](quickstart.md) -- Minimal store plugin tutorial
- [Naming Conventions](naming-conventions.md) -- How to name plugins, classes, files

### Concepts

- [Philosophy](philosophy.md) -- Design principles and mission
- [Introduction](introduction.md) -- Plugin system overview, 4 types, two-database system
- [Licensing](licensing.md) -- GPLv3 + plugin exception, import rules

### SDK Reference

- [SDK Overview](sdk/v1/docs/overview.md) -- Architecture, modules, injection
- [plugin.json Reference](sdk/v1/docs/plugin-json.md) -- Complete field reference

**SDK Modules:**

| Module | Purpose |
|--------|---------|
| [network](sdk/v1/docs/network.md) | HTTP client, domain enforcement |
| [storage](sdk/v1/docs/storage.md) | Path-confined filesystem access |
| [config](sdk/v1/docs/config.md) | Application config access |
| [json](sdk/v1/docs/json.md) | JSON with optional orjson |
| [datetime](sdk/v1/docs/datetime.md) | UTC timestamps, date parsing |
| [text](sdk/v1/docs/text.md) | Title normalization |
| [ui](sdk/v1/docs/ui.md) | Themed widgets and dialogs |
| [cookies](sdk/v1/docs/cookies.md) | Browser cookie access |
| [constants](sdk/v1/docs/constants.md) | App version, badge colors |
| [dialogs](sdk/v1/docs/dialogs.md) | Status helpers, login checking |

### Plugin Type Guides

| Type | Guide | Base Class |
|------|-------|------------|
| **Store** | [store-plugin.md](sdk/v1/docs/store-plugin.md) | `AbstractGameStore` |
| **Metadata** | [metadata-plugin.md](sdk/v1/docs/metadata-plugin.md) | `AbstractMetadataProvider` |
| **Platform** | [platform-plugin.md](sdk/v1/docs/platform-plugin.md) | `AbstractPlatformProvider` |
| **Runner** | [runner-plugin.md](sdk/v1/docs/runner-plugin.md) | `AbstractRunnerPlugin` |

### Advanced Topics

- [Authentication](sdk/v1/docs/authentication.md) -- 5 auth patterns
- [Error Handling](sdk/v1/docs/error-handling.md) -- Exception hierarchy
- [Security](sdk/v1/docs/security.md) -- Sandboxing and integrity
- [Testing](sdk/v1/docs/testing.md) -- pytest patterns and mocking

### Examples

| Example | Type | Description |
|---------|------|-------------|
| [GameVault](sdk/v1/examples/mock_store/) | Store | 5 games, API key auth, config actions |
| [GamePedia](sdk/v1/examples/mock_metadata/) | Metadata | Cross-reference lookup, enrichment |
| [PixelEngine](sdk/v1/examples/mock_platform/) | Platform | Engine detection, launch config |
| [GameVault Runner](sdk/v1/examples/mock_runner/) | Runner | Launcher detection, URI construction |

### Community

- [Submitting Guidelines](submitting-guidelines.md) -- Catalog requirements
- [FAQ](FAQ.md) -- Common questions
