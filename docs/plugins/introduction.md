# Plugin System Introduction

## Overview

Luducat's plugin system is how the application connects to the outside world.
Every store integration, every metadata source, every game runtime exists as a
plugin. The core application provides the framework -- database, UI, caching,
security -- and plugins provide the data and functionality.

## Plugin Types

There are four plugin types. Each serves a distinct role:

### Store Plugins

A **store plugin** imports game libraries from a storefront. It owns the
relationship between the user and their purchased games.

**Responsibilities:**
- Authenticate with the store (API key, OAuth, browser cookies)
- Fetch the user's owned game list
- Fetch game metadata (title, description, images, genres)
- Provide launch URLs for games
- Maintain a local catalog database (cache of all scraped data)

**Examples:** Steam, GOG, Epic Games

**Base class:** `AbstractGameStore` (alias: `StorePlugin`)

### Metadata Plugins

A **metadata plugin** enriches game data with information that store plugins
don't provide. It has no concept of game ownership.

**Responsibilities:**
- Look up games by store ID or title
- Return enrichment data (genres, tags, franchise, ratings, screenshots)
- Cache enrichment results in a local database
- Support batch enrichment during sync

**Examples:** IGDB, PCGamingWiki, SteamGridDB, ProtonDB

**Base class:** `AbstractMetadataProvider` (alias: `MetadataPlugin`)

### Platform Plugins

A **platform plugin** represents a game engine or platform that can run games.
Think of it as the answer to "what software do I need to run this game?"

**Responsibilities:**
- Detect installed platform versions (e.g., find DOSBox installations)
- Determine which games this platform can handle
- Create launch configurations (executable path, arguments, environment)

**Examples:** DOSBox (any DOS game), ScummVM (adventure games), OpenMW
(Morrowind engine replacement)

**Base class:** `AbstractPlatformProvider` (alias: `PlatformPlugin`)

### Runner Plugins

A **runner plugin** handles the actual game launch by delegating to a platform
or external application. It's the answer to "how do I start this game?"

**Responsibilities:**
- Detect whether the launcher application is installed
- Build launch URIs for specific games
- Execute the launch

**Examples:** Heroic Runner (launches GOG/Epic games via Heroic), Steam Client
Runner (launches via `steam://` URLs)

**Base class:** `AbstractRunnerPlugin` (alias: `RunnerPlugin`)

### How They Work Together

The four types form a stack:

```
Store       "I own this game"       (library, purchases, metadata)
Metadata    "Here's more info"      (genres, ratings, screenshots)
Platform    "This engine runs it"   (DOSBox, ScummVM, OpenMW)
Runner      "Launch it this way"    (Heroic, Steam Client, Lutris)
```

**Full-stack example:** A GOG DOS game might involve:
- **GOG store plugin** -- imports the game into the library
- **IGDB metadata plugin** -- enriches it with genres, franchise, ratings
- **DOSBox platform plugin** -- knows this game needs DOSBox to run
- **DOSBox runner plugin** -- launches the game through DOSBox

**Lightweight example:** A Steam game only needs:
- **Steam store plugin** -- imports and provides metadata
- **Steam runner plugin** -- handles launching via `steam://`

Not every game needs all four layers. The system composes what's available.

### Multi-Type Plugins

A single plugin can implement multiple types. For example, a Battle.net plugin
could be both a store (game library) and a runner (launcher). Declare multiple
types in `plugin.json`:

```json
{
  "plugin_types": ["store", "runner"]
}
```

## Plugin Discovery and Loading

### Directory Structure

Every plugin lives in its own directory:

```
my_store/
    __init__.py
    plugin.json      # Required: plugin metadata
    store.py         # Main implementation
    README.md        # Optional but recommended
```

### Discovery Flow

1. Luducat scans the plugins directory at startup
2. Each subdirectory with a `plugin.json` is a plugin candidate
3. The `plugin.json` is parsed into a `PluginMetadata` dataclass
4. Version compatibility is checked (`min_luducat_version`)
5. Import audit runs (telemetry blocked, core imports checked)
6. The plugin class is loaded and instantiated
7. Core services are injected (`http`, `storage`, credentials, etc.)

### Bundled vs User Plugins

**Bundled plugins** ship with luducat in `luducat/plugins/`. On first run,
they're copied to the user's plugin directory. Updates are automatic when the
bundled version is newer.

**User plugins** are placed directly in the plugins directory:
- Linux: `~/.local/share/luducat/plugins/`
- Windows: `%APPDATA%/luducat/plugins/`
- macOS: `~/Library/Application Support/luducat/plugins/`

### Plugin Sandboxing

Plugins run inside a security sandbox:

| Aspect | Enforcement |
|--------|-------------|
| **HTTP requests** | Must go through `PluginHttpClient`. Only declared domains allowed. |
| **File access** | Must go through `PluginStorage`. Confined to plugin directories. |
| **Credentials** | Must use `get_credential()` / `set_credential()`. System keyring backed. |
| **Imports** | Audited at load time. Telemetry libraries blocked. Core imports blocked for third-party. |
| **Integrity** | SHA-256 Merkle hash verified at startup. |

## The Two-Database System

This is a critical concept for plugin authors:

### Main Database (`games.db`)

The main database stores **user data**: game ownership, favorites, tags, hidden
status, launch counts, merged metadata. This is the user's personal catalog.

Plugins do not write directly to the main database. The core application
handles all writes based on data plugins provide.

### Plugin Databases (per-plugin)

Each plugin maintains its own SQLite database for caching. These databases are
**license-agnostic** -- they contain ALL games the plugin has ever scraped,
regardless of whether the user owns them.

For a store plugin, this means your catalog database might have 50,000 games
even though the user owns 200. That's by design. The catalog is a lookup cache
that enables metadata resolution for any game, not just owned ones.

**Convention:**
- Store plugins: `self.data_dir / "catalog.db"`
- Metadata plugins: `self.data_dir / "enrichment.db"`

## What the SDK Provides

The Plugin SDK (`luducat.plugins.sdk.*`) gives plugins access to:

| Module | Purpose |
|--------|---------|
| `network` | HTTP client with domain enforcement and rate limiting |
| `storage` | Path-confined filesystem access |
| `config` | Application config reading/writing |
| `json` | JSON serialization (with optional orjson acceleration) |
| `datetime` | UTC timestamps and release date parsing |
| `text` | Title normalization for cross-store deduplication |
| `ui` | Status labels, form groups, themed dialogs, icon tinting |
| `cookies` | Browser cookie access (privacy-gated) |
| `constants` | App version, user agent, badge colors |
| `dialogs` | Widget status, login checking, data reset |

All SDK modules are safe to import from any OSI-licensed plugin. They form the
GPL boundary -- see [Licensing](licensing.md) for details.

## Next Steps

- [Quickstart](quickstart.md) -- Build your first plugin in 10 minutes
- [Naming Conventions](naming-conventions.md) -- How to name things
- [SDK Overview](sdk/v1/docs/overview.md) -- SDK architecture deep dive
- [plugin.json Reference](sdk/v1/docs/plugin-json.md) -- Complete field reference
