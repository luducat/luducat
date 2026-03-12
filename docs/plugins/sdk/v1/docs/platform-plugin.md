# Building a Platform Plugin

A platform plugin represents a game engine or platform that can run games. It
answers the question: "what software do I need to run this game?"

**Base class:** `AbstractPlatformProvider` (alias: `PlatformPlugin`)

## What Is a Platform?

A platform is any software that executes games:
- **Emulators:** DOSBox (DOS games), ScummVM (classic adventure games)
- **Engine replacements:** OpenMW (Morrowind), OpenCorsixTH (Theme Hospital)
- **Game platforms:** Minecraft launchers, Flash/HTML5 engines
- **Compatibility layers:** Wine, Proton (Windows games on Linux)

The platform plugin detects installed engines, knows which games they can run,
and builds launch configurations. The actual launch may be handled by this
plugin directly or delegated to a [runner plugin](runner-plugin.md).

## Platform-Runner Relationship

Platforms and runners work together:

| Role | Question | Example |
|------|----------|---------|
| **Platform** | "What engine runs this game?" | DOSBox can run DOS games |
| **Runner** | "How do I start this game?" | Launch via DOSBox executable |

For simple cases, a single plugin can be both platform and runner:

```json
{"plugin_types": ["platform", "runner"]}
```

For complex cases, they're separate. A GOG DOS game might use:
- **DOSBox platform** -- knows the game needs DOSBox
- **DOSBox runner** -- launches the game through DOSBox with the right config

## Skeleton

```python
from pathlib import Path
from typing import Any, Dict, List

from luducat.plugins.base import AbstractPlatformProvider, Game


class MyPlatform(AbstractPlatformProvider):
    """My game platform."""

    @property
    def provider_name(self) -> str:
        return "my_platform"

    @property
    def display_name(self) -> str:
        return "My Platform"

    @property
    def platform_type(self) -> str:
        return "my_platform"

    def detect_platforms(self) -> List[Dict[str, Any]]:
        return []

    def can_run_game(self, game: Game) -> bool:
        return False

    def create_launch_config(self, game, platform_info, **kwargs):
        return {}
```

## Required Properties

### `provider_name -> str`

Unique identifier:

```python
@property
def provider_name(self) -> str:
    return "dosbox"
```

### `display_name -> str`

Human-readable name:

```python
@property
def display_name(self) -> str:
    return "DOSBox-Staging"
```

### `platform_type -> str`

Type classification. Used to match games to platforms:

```python
@property
def platform_type(self) -> str:
    return "dosbox"
```

Common types: `"dosbox"`, `"scummvm"`, `"wine"`, `"native"`,
`"external_launcher"`

## Required Methods

### `detect_platforms() -> List[Dict[str, Any]]`

Scan the system for installed platform versions:

```python
def detect_platforms(self) -> List[Dict[str, Any]]:
    platforms = []

    # Check common installation paths
    for name, path in [
        ("DOSBox-Staging", "/usr/bin/dosbox"),
        ("DOSBox-X", "/usr/bin/dosbox-x"),
    ]:
        exe = Path(path)
        if exe.exists():
            version = self._detect_version(exe)
            platforms.append({
                "platform_id": f"dosbox/{version}",
                "name": f"{name} {version}",
                "version": version,
                "executable_path": str(exe),
                "is_default": len(platforms) == 0,
                "is_managed": False,
            })

    # Check user-configured custom path
    custom = self.get_setting("custom_path")
    if custom and Path(custom).exists():
        platforms.append({
            "platform_id": "dosbox/custom",
            "name": "DOSBox (Custom)",
            "version": "unknown",
            "executable_path": custom,
            "is_default": len(platforms) == 0,
            "is_managed": False,
        })

    return platforms
```

Return format:

| Field | Type | Description |
|-------|------|-------------|
| `platform_id` | string | Unique ID for this platform instance |
| `name` | string | Display name with version |
| `version` | string | Version string |
| `executable_path` | string | Path to the platform executable |
| `is_default` | bool | Whether this is the preferred instance |
| `is_managed` | bool | Whether luducat installed this platform |

### `can_run_game(game) -> bool`

Check if this platform can handle the given game:

```python
def can_run_game(self, game: Game) -> bool:
    # Check game tags/categories for DOS indicators
    tags = [t.lower() for t in (game.tags or [])]
    categories = [c.lower() for c in (game.categories or [])]

    if "dos" in tags or "dos" in categories:
        return True

    # Check extra metadata
    extra = game.extra_metadata or {}
    if extra.get("platform") == "dos":
        return True

    return False
```

### `create_launch_config(game, platform_info, **kwargs) -> Dict[str, Any]`

Build the launch configuration:

```python
def create_launch_config(self, game, platform_info, **kwargs):
    game_path = kwargs.get("game_path")
    if not game_path:
        return {"launch_method": "error", "error": "No game path provided"}

    return {
        "launch_method": "executable",
        "executable": platform_info["executable_path"],
        "arguments": [
            "-conf", str(Path(game_path) / "dosbox.conf"),
            str(game_path),
        ],
        "working_directory": game_path,
        "environment": {},
    }
```

Launch config format:

| Field | Type | Description |
|-------|------|-------------|
| `launch_method` | string | `"url_scheme"`, `"executable"`, or `"command"` |
| `launch_url` | string | URL for `url_scheme` method |
| `executable` | string | Path for `executable`/`command` methods |
| `arguments` | string[] | Command-line arguments |
| `working_directory` | string | Working directory for the process |
| `environment` | dict | Environment variables to set |

## Optional Methods

### `launch(config) -> Dict[str, Any]`

The base class provides a default `launch()` implementation that handles URL
schemes and executables. Override only for custom launch behavior:

```python
def launch(self, config):
    # Custom launch logic
    result = super().launch(config)
    # Post-launch tracking
    return result
```

Return format: `{"success": bool, "error": str | None, "pid": int | None}`

### Settings Schemas

```python
def get_platform_settings_schema(self):
    """Global settings for this platform."""
    return {
        "fullscreen": {"type": "boolean", "default": True},
        "renderer": {"type": "choice", "choices": ["auto", "opengl", "vulkan"]},
    }

def get_game_settings_schema(self, game):
    """Per-game settings."""
    return {
        "cycles": {"type": "string", "default": "auto"},
        "mount_points": {"type": "string", "default": ""},
    }
```

### Lifecycle Hooks

```python
def on_enable(self):
    """Scan for platforms on enable."""
    self._platforms = self.detect_platforms()

def close(self):
    """Clean up."""
    ...
```

## Examples

### DOSBox Platform

Detects DOSBox-Staging and DOSBox-X installations. Can run any game tagged
with "dos" or detected as a DOS game. Creates launch configs with the
appropriate DOSBox executable and configuration files.

### ScummVM Platform

Detects ScummVM installation. Can run games from the ScummVM compatibility
list. Creates launch configs with ScummVM's game ID system.

### OpenMW Platform

Detects OpenMW installation. Can run Morrowind specifically. Creates launch
configs pointing to the Morrowind data directory.

## plugin.json

```json
{
  "name": "my_platform",
  "display_name": "My Platform",
  "version": "1.0.0",
  "author": "Your Name",
  "description": "Run games using My Platform engine",
  "min_luducat_version": "0.2.9.24",
  "plugin_types": ["platform"],
  "entry_point": "platform.MyPlatform",
  "capabilities": {
    "platform_type": "my_platform",
    "game_types": ["retro"],
    "launch_method": "executable",
    "managed_download": false
  },
  "network": {"allowed_domains": []},
  "privacy": {"telemetry": false, "data_collection": "none"},
  "settings_schema": {
    "custom_path": {"type": "path", "label": "Platform Path", "path_type": "file"}
  }
}
```

## Checklist

- [ ] All 3 required properties implemented
- [ ] All 3 required methods implemented
- [ ] `detect_platforms()` handles missing installations gracefully
- [ ] `can_run_game()` uses reliable detection (tags, metadata, file inspection)
- [ ] `create_launch_config()` produces valid configs for all detected platforms
- [ ] Settings schema provides user-configurable paths and options
- [ ] Tests cover detection, compatibility check, and config generation
