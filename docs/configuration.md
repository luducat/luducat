# Configuration

luducat uses TOML configuration files following XDG standards.

## File Locations (XDG Standards)

| Type | Path | Contents |
|------|------|----------|
| Config | `~/.config/luducat/config.toml` | User settings |
| Data | `~/.local/share/luducat/` | Databases, plugins |
| Cache | `~/.cache/luducat/` | Thumbnails, screenshots |

### Data Directory Structure
```
~/.local/share/luducat/
├── games.db                    # Main database
├── trust-state.json            # Plugin integrity trust data (HMAC-signed)
├── plugins/
│   ├── steam/
│   │   └── catalog.db          # Plugin database
│   ├── gog/
│   │   └── catalog.db
│   └── ...
└── backups/                    # Database backups
```

### Cache Directory Structure
```
~/.cache/luducat/
├── thumbnails/                 # Game cover images
└── screenshots/                # Screenshot images
```

## config.toml Structure

```toml
[app]
version = "0.5.0"               # App version that created this config
config_version = 7              # Config schema version (for migrations)
first_run = true
last_news_version = ""          # Track last viewed news version
check_for_updates = false       # Opt-in, default OFF
update_dismissed_version = ""   # Version user dismissed (don't nag again)
language = ""                   # "" = auto-detect, or "en", "de", "fr", "es", "it"
custom_data_dir = ""            # Empty = use XDG default
custom_cache_dir = ""           # Empty = use XDG default

[ui]
window_width = 1200
window_height = 800
window_maximized = false
list_panel_width = 280
view_mode = "cover"             # list, cover, screenshot
sort_mode = "name"
sort_reverse = false
favorites_first = false

[appearance]
ui_zoom = 100                   # percentage: 50-400
theme_override = "auto"         # auto, light, dark
cover_grid_density = 150
screenshot_grid_density = 250
cover_scaling = "stretch"
quick_tag_count = 5             # Max quick-access tags in crumb bar

[filters]
quick_filter = "all"            # all, favorites, recent
active_stores = []              # Empty = all stores
active_tags = []

[sync]
auto_sync_on_startup = false
preferred_browser = "auto"      # auto, firefox, chrome, chromium, brave, vivaldi, edge, opera

[privacy]
local_data_access_consent = false  # Consent for browser cookies + local launcher data

[content_filter]
enabled = true                  # Hide games that exceed adult confidence threshold
threshold = 0.60                # 0.0–1.0; lower = stricter, higher = more permissive

[tags]
default_sync_mode = "add_only"  # "add_only" | "full_sync" | "none"
source_colors = false           # Show brand color accent on imported tags
suppress_deleted_reimport = true
plugin_overrides = {}           # Per-plugin: {steam = {enabled, sync_mode}, ...}
suppressed_imports = []

[cache]
thumbnail_max_size_mb = 500
screenshot_max_size_mb = 2000
cache_cleanup_days = 30
ram_cache_mode = "auto"         # "auto" | "manual"
ram_cache_manual_mb = 0         # Manual budget in MB (0 = use auto)

[backup]
schedule_enabled = false
interval_days = 1
check_on_startup = true
silent = false
location = ""                   # Empty = default backup location
last_backup = ""
retention_daily = 7
retention_weekly = 4
retention_monthly = 12
retention_yearly = 1

[desktop]
icons_installed = false         # Set true after installing icons to hicolor theme

[game_manager]
install_root = ""               # Default game installation directory
auto_install_on_launch = false
verify_before_launch = false
show_first_run_dialog = true

[runtime_manager]
auto_detect_platforms = true    # Auto-detect installed platforms
download_missing_platforms = true

[metadata_priority]
# Per-field priorities — seeded from defaults on first run.
# Each field maps to a list of sources in priority order (highest first).
# Configurable via Settings → Plugins → Metadata Priority.
```

## Plugin Configuration

Plugin settings are stored dynamically under `[plugins.{plugin_name}]` sections.
These are created when plugins are enabled and configured. Each plugin defines
its available settings in `plugin.json`. Secret fields (like API keys) use the
system credential manager, not config.toml.
