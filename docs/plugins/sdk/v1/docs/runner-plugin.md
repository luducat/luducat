# Building a Runner Plugin

A runner plugin handles the actual game launch by delegating to a platform or
external application. It answers: "how do I start this game?"

**Base class:** `AbstractRunnerPlugin` (alias: `RunnerPlugin`)

## What Is a Runner?

A runner connects luducat to an external system that handles game launching:
- **Store launchers:** Steam Client, GOG Galaxy, Epic Games Launcher
- **Third-party launchers:** Heroic, Lutris, Minigalaxy, Bottles
- **Platform runners:** Playnite via IPC

The key distinction from other plugin types:

| Type | Owns | Examples |
|------|------|---------|
| **Store** | Game data (library, purchases, metadata) | Steam, GOG, Epic |
| **Platform** | Engine capability (what can run the game) | DOSBox, ScummVM |
| **Runner** | Launch execution (how to start the game) | Heroic, Steam Client |

## Runner-Store-Platform Relationship

**Full-stack example:** A Battle.net game might use:
- **Battle.net store** -- game data and library
- **Battle.net platform** -- manages the client/shim
- **Battle.net runner** -- launches via Battle.net application

**Lightweight example:** An OpenMW game:
- **GOG store** -- game ownership (Morrowind purchased on GOG)
- **OpenMW platform** -- the engine that runs it
- **OpenMW runner** -- launches through the OpenMW executable

No store plugin needed for OpenMW itself -- GOG/Steam handle ownership.
No metadata plugin needed -- IGDB/PCGamingWiki already cover Morrowind.

## Launch Types

Runner plugins use four launch-related types. Import them from
`luducat.plugins.base` — not from `luducat.core`:

```python
from luducat.plugins.base import (
    AbstractRunnerPlugin,
    RunnerLauncherInfo,    # Launcher detection result
    LaunchIntent,          # What to launch
    LaunchMethod,          # How to launch (URL_SCHEME, EXECUTABLE, COMMAND, IPC)
    LaunchResult,          # Launch outcome
)
```

| Type | Purpose |
|------|---------|
| `RunnerLauncherInfo` | Returned by `detect_launcher()` -- describes the found launcher |
| `LaunchIntent` | Built by `build_launch_intent()` -- separates what from how |
| `LaunchMethod` | Enum: `URL_SCHEME`, `EXECUTABLE`, `COMMAND`, `IPC`, `REROUTE` |
| `LaunchResult` | Returned by `execute_launch()` -- success/failure with details |

### RunnerLauncherInfo Fields

Returned by `detect_launcher()` to describe a found launcher:

| Field | Type | Description |
|-------|------|-------------|
| `runner_name` | `str` | Runner identifier (matches `self.runner_name`) |
| `path` | `Optional[Path]` | Executable path (`None` for Flatpak or URL-only) |
| `install_type` | `str` | `"system"`, `"flatpak"`, `"appimage"`, `"registry"`, `"bundle"` |
| `virtualized` | `bool` | Whether the launcher runs in a sandbox |
| `version` | `Optional[str]` | Detected version string |
| `url_scheme` | `Optional[str]` | URI scheme if registered (e.g. `"heroic://"`) |
| `flatpak_id` | `Optional[str]` | Flatpak application ID |
| `capabilities` | `Dict[str, Any]` | Runner-specific feature flags |

### LaunchIntent Fields

Built by `build_launch_intent()` to describe how to launch a game:

| Field | Type | Description |
|-------|------|-------------|
| `method` | `LaunchMethod` | How to launch (`URL_SCHEME`, `EXECUTABLE`, etc.) |
| `runner_name` | `str` | Runner identifier |
| `store_name` | `str` | Store the game belongs to |
| `app_id` | `str` | Store-specific app ID |
| `url` | `Optional[str]` | URI for `URL_SCHEME` method |
| `executable` | `Optional[Path]` | Binary path for `EXECUTABLE` method |
| `arguments` | `List[str]` | Command-line arguments |
| `environment` | `Dict[str, str]` | Environment variables |
| `working_directory` | `Optional[Path]` | Working directory for the process |

## Skeleton

```python
from pathlib import Path
from typing import Dict, List, Optional

from luducat.plugins.base import (
    AbstractRunnerPlugin,
    RunnerLauncherInfo,
    LaunchIntent,
    LaunchMethod,
)


class MyRunner(AbstractRunnerPlugin):
    """My launcher runner."""

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._launcher_info: Optional[RunnerLauncherInfo] = None
        self._detection_done = False

    @property
    def runner_name(self) -> str:
        return "my_runner"

    @property
    def display_name(self) -> str:
        return "My Runner"

    @property
    def supported_stores(self) -> List[str]:
        return ["gog", "epic"]

    def detect_launcher(self) -> Optional[RunnerLauncherInfo]:
        if self._detection_done:
            return self._launcher_info
        self._detection_done = True

        from luducat.plugins.sdk.app_finder import find_application
        results = find_application(["my_launcher"])
        if not results:
            return None

        r = results[0]
        self._launcher_info = RunnerLauncherInfo(
            runner_name=self.runner_name,
            path=r.path,
            install_type=r.install_type,
            virtualized=r.virtualized,
        )
        return self._launcher_info

    def build_launch_intent(
        self, store_name: str, app_id: str
    ) -> Optional[LaunchIntent]:
        if store_name not in self.supported_stores:
            return None
        info = self.detect_launcher()
        if not info:
            return None
        return LaunchIntent(
            method=LaunchMethod.URL_SCHEME,
            runner_name=self.runner_name,
            store_name=store_name,
            app_id=app_id,
            url=f"my-runner://launch/{store_name}/{app_id}",
        )
```

## Required Properties

### `runner_name -> str`

Unique identifier:

```python
@property
def runner_name(self) -> str:
    return "heroic"
```

### `display_name -> str`

Human-readable name:

```python
@property
def display_name(self) -> str:
    return "Heroic Runner"
```

### `supported_stores -> List[str]`

Which store plugins this runner can launch games for:

```python
@property
def supported_stores(self) -> List[str]:
    return ["gog", "epic"]
```

## Required Methods

### `detect_launcher() -> Optional[RunnerLauncherInfo]`

Detect the launcher and return info about it, or `None` if not installed.
Cache the result to avoid repeated filesystem checks:

```python
def detect_launcher(self) -> Optional[RunnerLauncherInfo]:
    """Detect launcher installation.

    Returns:
        RunnerLauncherInfo with path, install_type, capabilities,
        or None if the launcher is not found.
    """
    if self._detection_done:
        return self._launcher_info
    self._detection_done = True

    from luducat.plugins.sdk.app_finder import find_application

    results = find_application(
        ["heroic"],
        flatpak_ids=["com.heroicgameslauncher.hgl"],
    )
    if not results:
        return None

    r = results[0]
    self._launcher_info = RunnerLauncherInfo(
        runner_name=self.runner_name,
        path=r.path,
        install_type=r.install_type,
        virtualized=r.virtualized,
        flatpak_id=r.flatpak_id,
        capabilities={
            "stores": self.supported_stores,
        },
    )
    return self._launcher_info
```

The SDK provides `find_application()` to search for binaries across
system paths, AppImages, Flatpak, and custom directories.

### `build_launch_intent(store_name, app_id) -> Optional[LaunchIntent]`

Build a structured launch intent describing how to start the game.
Return `None` if this runner cannot launch the given game:

```python
def build_launch_intent(
    self, store_name: str, app_id: str
) -> Optional[LaunchIntent]:
    """Build launch intent for a game.

    Args:
        store_name: luducat store name (e.g. "steam", "gog").
        app_id: Store-specific app ID string.

    Returns:
        LaunchIntent describing how to launch, or None if
        this runner cannot launch the given game.
    """
    if store_name not in self.supported_stores:
        return None

    info = self.detect_launcher()
    if not info:
        return None

    # URL scheme launch (when handler is registered)
    if info.url_scheme:
        return LaunchIntent(
            method=LaunchMethod.URL_SCHEME,
            runner_name=self.runner_name,
            store_name=store_name,
            app_id=app_id,
            url=f"heroic://launch/{store_name}/{app_id}",
        )

    # Direct binary launch (when no URL handler)
    if info.path:
        return LaunchIntent(
            method=LaunchMethod.EXECUTABLE,
            runner_name=self.runner_name,
            store_name=store_name,
            app_id=app_id,
            executable=info.path,
            arguments=["--launch", f"{store_name}/{app_id}"],
        )

    return None
```

The `LaunchIntent` separates *what to launch* from *how to execute it*.
The base class provides `execute_launch()` which handles URL and binary
launches automatically. Override it only for custom execution logic.

## Optional Methods

### `build_install_url(store_name, app_id) -> Optional[str]`

Return an install URL for games not yet installed through this launcher:

```python
def build_install_url(self, store_name: str, app_id: str) -> Optional[str]:
    return f"heroic://install/{store_name}/{app_id}"
```

### Lifecycle Hooks

```python
def on_enable(self):
    """Called when plugin is enabled."""
    info = self.detect_launcher()
    if not info:
        import logging
        logging.getLogger(__name__).info(
            f"{self.display_name}: launcher not detected"
        )

def close(self):
    """Clean up resources."""
    ...

def clear_cache(self):
    """Reset detection state for re-scanning."""
    self._launcher_info = None
    self._detection_done = False
```

## Injected Properties

| Property | Type | Usage |
|----------|------|-------|
| `self.http` | `PluginHttpClient` | For runners that need HTTP (rare) |
| `self.storage` | `PluginStorage` | For runners that cache state |
| `self.config_dir` | `Path` | Plugin config directory |
| `self.cache_dir` | `Path` | Plugin cache directory |
| `self.data_dir` | `Path` | Plugin data directory |

Settings: `self.get_setting(key, default=None)`

## plugin.json

```json
{
  "name": "my_runner",
  "display_name": "My Runner",
  "version": "1.0.0",
  "author": "Your Name",
  "description": "Launch games through My Launcher",
  "min_luducat_version": "0.2.9.24",
  "plugin_types": ["runner"],
  "entry_point": "runner.MyRunner",
  "capabilities": {
    "supported_stores": ["gog", "epic"],
    "launch_method": "url_scheme"
  },
  "network": {"allowed_domains": []},
  "privacy": {"telemetry": false, "data_collection": "none"}
}
```

## Multi-Type Plugin Example

A plugin that's both a runner and a store:

```json
{
  "plugin_types": ["store", "runner"],
  "store_class": "store.MyStore",
  "entry_point": "runner.MyRunner"
}
```

The store class handles library/metadata. The runner class handles launching.
They can share data through the plugin's storage directory.

## Checklist

- [ ] All 3 required properties implemented
- [ ] `detect_launcher()` returns `RunnerLauncherInfo` or `None`
- [ ] `build_launch_intent()` returns `LaunchIntent` or `None`
- [ ] Detection result is cached (avoid repeated filesystem scans)
- [ ] `supported_stores` accurately reflects capabilities
- [ ] Cross-platform path handling (don't assume Linux-only)
