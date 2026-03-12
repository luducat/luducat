# plugin.json Reference

Every plugin must include a `plugin.json` file in its root directory. This file
declares the plugin's identity, capabilities, dependencies, authentication,
network access, and settings schema.

## Required Fields

These fields must be present in every `plugin.json`:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique identifier. Lowercase, alphanumeric + underscores. |
| `display_name` | string | Human-readable name for UI display. |
| `version` | string | Semantic version (MAJOR.MINOR.PATCH). |
| `author` | string | Author name. |
| `description` | string | Short description of the plugin. |
| `min_luducat_version` | string | Minimum luducat version required. |

### Plugin Type Fields (one required)

| Field | Type | Used By |
|-------|------|---------|
| `plugin_types` | string[] | All plugins. Values: `"store"`, `"metadata"`, `"platform"`, `"runner"` |
| `store_class` | string | Store plugins. Module path to class (e.g. `"store.SteamStore"`). |
| `provider_class` | string | Metadata plugins. Module path (e.g. `"provider.IgdbProvider"`). |
| `entry_point` | string | Platform/runner plugins. Module path (e.g. `"provider.DOSBoxProvider"`). |

## Optional Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_luducat_version` | string | `null` | Maximum luducat version supported. |
| `author_email` | string | `null` | Author's email address. |
| `homepage` | string | `null` | Project URL. |
| `icon` | string | `null` | Relative path to icon file. |
| `badge_label` | string | `""` | Short label for store badges (e.g. `"STEAM"`). |
| `enabled_by_default` | boolean | varies | Whether plugin starts enabled. |
| `config_dialog_class` | string | `null` | Custom config dialog class path. |

## `dependencies`

Declare Python version and package requirements:

```json
{
  "dependencies": {
    "python": ">=3.11",
    "packages": ["requests>=2.28.0", "aiohttp>=3.8.0"]
  }
}
```

## `platforms`

Declare platform support:

```json
{
  "platforms": {
    "linux": true,
    "windows": true,
    "macos": false
  }
}
```

Defaults to all platforms enabled.

## `capabilities`

Declare what the plugin can do. The structure differs by plugin type.

### Store Plugin Capabilities

```json
{
  "capabilities": {
    "fetch_library": true,
    "fetch_metadata": true,
    "launch_games": true,
    "track_playtime": false,
    "achievements": false,
    "cloud_saves": false,
    "download_info": false
  }
}
```

### Metadata Plugin Capabilities

```json
{
  "capabilities": {
    "enrich_metadata": true,
    "search_games": true,
    "fetch_metadata": true,
    "genres": true,
    "tags": true,
    "franchises": true,
    "ratings": true,
    "screenshots": true,
    "tag_sync": false
  }
}
```

The boolean fields (genres, tags, etc.) declare which enrichment data types
the plugin provides. This is used by the UI to show/hide relevant options.

### Platform Plugin Capabilities

```json
{
  "capabilities": {
    "platform_type": "dosbox",
    "game_types": ["dos"],
    "launch_method": "executable",
    "managed_download": true,
    "status": "in_development"
  }
}
```

## `brand_colors`

Define colors for UI badges and store indicators:

```json
{
  "brand_colors": {
    "bg": "#1b2838",
    "text": "#66c0f4"
  }
}
```

Use the store's official brand colors.

## `auth`

Declare the authentication method:

### No Authentication

```json
{
  "auth": {
    "type": "none"
  }
}
```

### API Key

```json
{
  "auth": {
    "type": "api_key",
    "fields": ["api_key"],
    "help_url": "https://example.com/api-keys",
    "help_text": "Get API Key"
  }
}
```

### OAuth

```json
{
  "auth": {
    "type": "oauth",
    "fields": ["client_id", "client_secret"],
    "optional": true,
    "help_url": "https://dev.example.com/console",
    "help_text": "Optional: provide your own credentials"
  }
}
```

### Browser Cookies

```json
{
  "auth": {
    "type": "browser_cookies",
    "help_text": "Uses browser cookies from example.com"
  }
}
```

### External Tool

```json
{
  "auth": {
    "type": "external_tool",
    "tool_name": "legendary",
    "help_text": "Uses Legendary CLI for authentication"
  }
}
```

## `settings_schema`

Define configuration fields shown in the plugin's settings dialog. Each key
is a setting name, and the value describes its type and behavior:

### String Setting

```json
{
  "api_key": {
    "type": "string",
    "label": "API Key",
    "description": "Your API key from example.com",
    "secret": true,
    "required": true,
    "placeholder": "Enter key..."
  }
}
```

### Boolean Setting

```json
{
  "auto_enrich": {
    "type": "boolean",
    "label": "Auto-enrich during sync",
    "description": "Automatically fetch metadata during store sync",
    "default": true
  }
}
```

### Number Setting

```json
{
  "rate_limit_delay": {
    "type": "number",
    "label": "API delay (seconds)",
    "description": "Delay between API requests",
    "default": 0.5
  }
}
```

### Choice Setting

```json
{
  "hero_style": {
    "type": "choice",
    "label": "Preferred style",
    "description": "Filter images by visual style",
    "choices": ["Any", "Alternate", "Blurred"],
    "default": "Any"
  }
}
```

### Path Setting

```json
{
  "custom_path": {
    "type": "path",
    "label": "Custom Path",
    "description": "Path to executable",
    "path_type": "file",
    "file_filter": "AppImage (*.AppImage);;All Files (*)"
  }
}
```

`path_type` can be `"file"` or `"directory"`.

### Dynamic Choices

```json
{
  "launcher": {
    "type": "choice",
    "label": "Preferred Launcher",
    "dynamic_choices": "get_available_launchers",
    "default": "auto"
  }
}
```

`dynamic_choices` names a method on the plugin class that returns a list of
available options at runtime.

### Advanced Settings

Settings with `"advanced": true` are hidden behind an "Advanced" expander:

```json
{
  "proxy_url": {
    "type": "string",
    "label": "Proxy URL",
    "default": "https://proxy.example.com",
    "advanced": true
  }
}
```

## `config_actions`

Declare buttons in the plugin's config dialog:

```json
{
  "config_actions": [
    {
      "id": "sync_data",
      "label": "Sync Data...",
      "callback": "_open_sync_dialog",
      "group": "data"
    },
    {
      "id": "test_connection",
      "label": "Test Connection",
      "callback": "_test_connection",
      "group": "auth",
      "icon": "refresh.svg",
      "requires_auth": true
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique action identifier. |
| `label` | string | Button text (translated at render time). |
| `callback` | string | Method name on the plugin class. |
| `group` | string | Layout group: `"auth"`, `"data"`, or `"general"`. |
| `icon` | string | Optional SVG filename from `assets/icons/`. |
| `requires_auth` | boolean | Disable button when not authenticated. |
| `dialog_class` | string | Optional dialog class to open on click. |

## `provides_fields`

Declare which metadata fields this plugin can provide, with priority values:

```json
{
  "provides_fields": {
    "title": {"priority": 10},
    "description": {"priority": 20},
    "genres": {"priority": 30},
    "cover": {"priority": 10},
    "screenshots": {"priority": 20}
  }
}
```

**Priority values:** Lower numbers = higher priority. When multiple plugins
provide the same field, the one with the lowest priority number wins.

Typical ranges:
- 10: Primary source for this field
- 20-30: Good source, secondary
- 40-50: Fallback source

See `CANONICAL_METADATA_FIELDS` in `luducat/plugins/base.py` for the full
list of recognized field names.

## `network`

Declare network access requirements:

```json
{
  "network": {
    "allowed_domains": [
      "api.example.com",
      "cdn.example.com"
    ],
    "rate_limits": {
      "api.example.com": {"requests": 5, "window": 1},
      "cdn.example.com": {"requests": 10, "window": 1}
    }
  }
}
```

| Field | Description |
|-------|-------------|
| `allowed_domains` | Domains the plugin may access. All others are blocked. |
| `rate_limits` | Per-domain rate limits. `requests` per `window` (seconds). |

Plugins with no network needs should declare an empty allowlist:

```json
{
  "network": {
    "allowed_domains": []
  }
}
```

## `privacy`

Declare privacy-related behavior:

```json
{
  "privacy": {
    "telemetry": false,
    "data_collection": "none",
    "third_party_services": []
  }
}
```

All plugins **must** declare `"telemetry": false`. Any plugin declaring
telemetry will be flagged during review.

## `credentials`

Configure credential storage:

```json
{
  "credentials": {
    "use_system_keyring": true,
    "keyring_service": "luducat.my_store"
  }
}
```

## Complete Example: Store Plugin

```json
{
  "name": "gamevault",
  "display_name": "GameVault",
  "version": "1.0.0",
  "author": "Your Name",
  "author_email": "you@example.com",
  "description": "GameVault store integration",
  "homepage": "https://github.com/you/luducat-gamevault",
  "min_luducat_version": "0.2.9.24",
  "plugin_types": ["store"],
  "store_class": "store.GameVaultStore",
  "badge_label": "GV",
  "brand_colors": {"bg": "#2a4a2a", "text": "#90ee90"},
  "dependencies": {
    "python": ">=3.11",
    "packages": ["requests>=2.28.0"]
  },
  "platforms": {"linux": true, "windows": true, "macos": true},
  "capabilities": {
    "fetch_library": true,
    "fetch_metadata": true,
    "launch_games": true,
    "track_playtime": false
  },
  "provides_fields": {
    "title": {"priority": 20},
    "description": {"priority": 20},
    "cover": {"priority": 30},
    "screenshots": {"priority": 30}
  },
  "auth": {
    "type": "api_key",
    "fields": ["api_key"],
    "help_url": "https://gamevault.example.com/settings/api",
    "help_text": "Get GameVault API Key"
  },
  "settings_schema": {
    "api_key": {
      "type": "string",
      "label": "API Key",
      "secret": true,
      "required": true
    }
  },
  "network": {
    "allowed_domains": ["api.gamevault.example.com", "cdn.gamevault.example.com"],
    "rate_limits": {
      "api.gamevault.example.com": {"requests": 10, "window": 1}
    }
  },
  "privacy": {
    "telemetry": false,
    "data_collection": "none",
    "third_party_services": []
  },
  "credentials": {
    "use_system_keyring": true,
    "keyring_service": "luducat.gamevault"
  }
}
```

## Complete Example: Metadata Plugin

```json
{
  "name": "gamepedia",
  "display_name": "GamePedia",
  "version": "1.0.0",
  "author": "Your Name",
  "description": "GamePedia metadata enrichment",
  "min_luducat_version": "0.2.9.24",
  "plugin_types": ["metadata"],
  "provider_class": "provider.GamePediaProvider",
  "capabilities": {
    "enrich_metadata": true,
    "search_games": true,
    "genres": true,
    "tags": true,
    "franchises": true
  },
  "provides_fields": {
    "genres": {"priority": 15},
    "franchise": {"priority": 15},
    "themes": {"priority": 20}
  },
  "auth": {"type": "api_key", "fields": ["api_key"]},
  "settings_schema": {
    "api_key": {"type": "string", "label": "API Key", "secret": true, "required": true},
    "auto_enrich": {"type": "boolean", "label": "Auto-enrich", "default": true}
  },
  "network": {
    "allowed_domains": ["api.gamepedia.example.com"],
    "rate_limits": {"api.gamepedia.example.com": {"requests": 5, "window": 1}}
  },
  "privacy": {"telemetry": false, "data_collection": "none", "third_party_services": []}
}
```

## Complete Example: Platform Plugin

```json
{
  "name": "pixelengine",
  "display_name": "PixelEngine",
  "description": "Run retro games using the PixelEngine platform",
  "version": "1.0.0",
  "author": "Your Name",
  "min_luducat_version": "0.2.9.24",
  "plugin_types": ["platform"],
  "entry_point": "platform.PixelEnginePlatform",
  "capabilities": {
    "platform_type": "pixelengine",
    "game_types": ["retro", "pixel"],
    "launch_method": "executable",
    "managed_download": false
  },
  "network": {"allowed_domains": []},
  "privacy": {"telemetry": false, "data_collection": "none", "third_party_services": []},
  "settings_schema": {
    "custom_path": {"type": "path", "label": "Engine Path", "path_type": "file"}
  }
}
```

## Complete Example: Runner Plugin

```json
{
  "name": "gamevault_runner",
  "display_name": "GameVault Runner",
  "description": "Launch games through the GameVault application",
  "version": "1.0.0",
  "author": "Your Name",
  "min_luducat_version": "0.2.9.24",
  "plugin_types": ["runner"],
  "entry_point": "runner.GameVaultRunner",
  "capabilities": {
    "supported_stores": ["gamevault"],
    "launch_method": "url_scheme"
  },
  "network": {"allowed_domains": []},
  "privacy": {"telemetry": false, "data_collection": "none", "third_party_services": []}
}
```
