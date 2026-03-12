# Plugin FAQ

## Getting Started

### How do I create a plugin?

Use the [Quickstart](quickstart.md) guide for a 10-minute walkthrough, or
run the [Plugin Generator](generator/generate_plugin.py):

```bash
python docs/plugins/generator/generate_plugin.py --name my_store --type store
```

### Where do I put my plugin?

Copy the plugin directory to your luducat plugins folder:
- Linux: `~/.local/share/luducat/plugins/`
- Windows: `%APPDATA%/luducat/plugins/`
- macOS: `~/Library/Application Support/luducat/plugins/`

### My plugin doesn't appear in Settings. What's wrong?

Check the log file at `~/.local/share/luducat/luducat.log` for:
- **JSON parse errors** -- validate your `plugin.json`
- **Import audit blocked** -- you're importing from `luducat.core`
- **Version mismatch** -- your `min_luducat_version` is higher than the
  installed version

### Can I test my plugin without restarting luducat?

Currently, plugins are loaded at startup. You need to restart luducat to
pick up changes. A plugin reload feature is planned.

## SDK

### Can I use `requests` directly?

No. All HTTP requests must go through `self.http` (the injected
`PluginHttpClient`). This ensures domain enforcement, rate limiting, and
request tracking. The import audit won't catch `requests` usage at load time
(it's a third-party package), but the NetworkManager won't track your requests.

### Can I import from `luducat.core`?

**Third-party plugins:** No. The import audit blocks `from luducat.core`
imports. This is the GPL boundary -- importing core makes your plugin a GPL
derivative.

**Bundled plugins:** Yes, with a warning. Bundled plugins ship with luducat
and are already GPLv3.

### What if I need something from core?

File a feature request. If it's useful for plugin authors, we'll expose it
through the SDK. The SDK is designed to grow based on real needs.

### Why does `self.http` return `None`?

Properties like `self.http` and `self.storage` are injected by the plugin
manager after `__init__()`. They're `None` during construction. Use them
inside methods, not at module level or in `__init__`.

### Can I use async/await?

Yes. The base class methods like `fetch_user_games()` and `authenticate()`
are `async`. The sync system calls them from background threads using
`asyncio.run()`.

### How do I store persistent data?

Use `self.storage` for file-based storage or create a SQLite database via
`self.storage.get_db_path()`. Data goes in the plugin's data directory
(survives cache clears).

## Security

### What gets audited?

At load time, every `.py` file in your plugin directory is scanned for:
1. Telemetry library imports (always blocked)
2. `luducat.core` imports (blocked for third-party)

At runtime:
- HTTP requests checked against your domain allowlist
- File operations confined to your plugin directories
- Credential access goes through the system keyring

### What if my plugin needs a domain I forgot to declare?

The request will fail with a domain enforcement error. Add the domain to
`network.allowed_domains` in your `plugin.json`.

### How is plugin integrity verified?

A SHA-256 Merkle hash of all `.py` files and `plugin.json` is computed at
startup and compared against known-good hashes. See
[Security](sdk/v1/docs/security.md) for details.

### Can users see my plugin's network activity?

Yes. Request statistics are tracked per plugin and per domain. Users can see
how many requests your plugin made and to which domains via the developer
console.

## Distribution

### How do I submit my plugin to the catalog?

See [Submitting Guidelines](submitting-guidelines.md). You need a public git
repository, an OSI license, and compliance with the non-negotiables.

### Can I sell a plugin?

Yes. Commercial plugins are welcome as long as they use an OSI license and
meet the catalog requirements.

### How do users install my plugin?

Currently: download the plugin directory and copy it to the plugins folder.
An in-app plugin browser is planned for a future release.

### How do updates work?

When you bump the version in `plugin.json`, luducat detects the newer version
and updates the installed copy. For bundled plugins, this happens automatically.
For user plugins, the user needs to update manually (automated update checking
is planned).

## Plugin Types

### Can a plugin be both a store and a metadata provider?

Yes. Declare multiple types in `plugin.json`:

```json
{"plugin_types": ["store", "metadata"]}
```

Implement both base classes in separate files.

### What's the difference between a runner and a platform?

A **platform** is a game engine or platform that can run games (DOSBox, ScummVM,
Wine). It answers "what software runs this game?"

A **runner** handles the actual launch by delegating to a platform or external
application (Heroic, Lutris). It answers "how do I start this game?"

They often come in pairs: DOSBox platform + DOSBox runner.

### Do I need a runner for every store plugin?

Not always. luducat's bundled runner plugins (Steam, Heroic, etc.) handle most
stores. A separate runner is only needed when:
- A third-party launcher handles games from your store
- Multiple stores share one launcher (e.g., Heroic for GOG + Epic)
- You want custom launch mechanics beyond URL schemes

### When should I write a platform vs a runner?

| Scenario | Type |
|----------|------|
| You provide a game engine | Platform |
| You detect and launch through an external app | Runner |
| You provide an engine AND handle launching | Both (multi-type) |

## Advanced

### How does metadata priority work?

Each plugin declares `provides_fields` with priority numbers. Lower number =
higher priority. When multiple plugins provide the same field, the lowest
priority number wins.

The user can customize priority order in Settings > Plugins > Metadata Priority.

### What's the two-database system?

1. **Main DB** (`games.db`): User data -- ownership, favorites, tags
2. **Plugin DBs** (per-plugin): License-agnostic caches of all scraped data

Your plugin database should contain ALL games you've ever fetched, not just
user-owned ones. This enables metadata resolution for any game.

### How do I handle rate limiting?

Raise `RateLimitError` with `wait_seconds`. Don't sleep inside your plugin:

```python
raise RateLimitError(message="Rate limited", wait_seconds=60, reason="429")
```

The sync system handles retry, cooldown, and user notification.

### Can I access other plugins' data?

No. Plugins are sandboxed. You can't read another plugin's database or
settings. If you need cross-plugin data, use the metadata priority system
(the core handles field resolution across plugins).

### How do I debug my plugin?

1. Check the log: `~/.local/share/luducat/luducat.log`
2. Run with `--debug` flag for verbose logging
3. Use `logger.debug()` / `logger.info()` in your plugin
4. Request stats: `self.http.get_stats()` shows network activity
