# Naming Conventions

Consistent naming makes plugins discoverable and avoids conflicts. Follow
these conventions for anything that becomes an identifier.

## Plugin Name (`name` in plugin.json)

The unique identifier used internally for database keys, config sections,
credential storage, and file paths.

**Rules:**
- Lowercase only
- Alphanumeric characters and underscores
- No hyphens, spaces, or special characters
- Must be unique across all installed plugins

**Examples:**

| Good | Bad | Why |
|------|-----|-----|
| `steam` | `Steam` | Lowercase only |
| `gog` | `GOG` | Lowercase only |
| `epic` | `epic-games` | No hyphens |
| `itch_io` | `itch.io` | No dots |
| `pcgamingwiki` | `PCGamingWiki` | Lowercase only |
| `my_store` | `my store` | No spaces |

## Display Name (`display_name` in plugin.json)

The human-readable name shown in the UI. Can contain any characters.

**Examples:** `"Steam"`, `"GOG"`, `"Epic Games"`, `"PCGamingWiki"`,
`"SteamGridDB"`, `"DOSBox"`

## Class Names

Follow Python conventions: PascalCase, descriptive.

| Plugin Type | Class Pattern | Example |
|-------------|--------------|---------|
| Store | `{Name}Store` | `SteamStore`, `GogStore`, `EpicStore` |
| Metadata | `{Name}Provider` | `IgdbProvider`, `PcgwProvider`, `SgdbProvider` |
| Platform | `{Name}Provider` | `DOSBoxProvider`, `ScummVMProvider` |
| Runner | `{Name}Runner` | `HeroicRunner`, `LutrisRunner` |

## File Names

| File | Convention | Examples |
|------|-----------|---------|
| Main store class | `store.py` | `store.py` |
| Main metadata class | `provider.py` | `provider.py` |
| Main platform class | `provider.py` | `provider.py` |
| Main runner class | `runner.py` | `runner.py` |
| Plugin metadata | `plugin.json` | `plugin.json` |
| Package init | `__init__.py` | `__init__.py` |
| Config dialog (custom) | `config_dialog.py` | `config_dialog.py` |
| Database models | `models.py` | `models.py` |

## Directory Names

Plugin directories match the plugin name:

```
plugins/
    steam/               # name: "steam"
    gog/                 # name: "gog"
    epic/                # name: "epic"
    igdb/                # name: "igdb"
    pcgamingwiki/        # name: "pcgamingwiki"
    steamgriddb/         # name: "steamgriddb"
    heroic/              # name: "heroic"
    platforms/
        dosbox/          # name: "dosbox"
        scummvm/         # name: "scummvm"
        wine/            # name: "wine"
```

Platform plugins live under `platforms/` for organizational clarity. The plugin
name is still just the leaf directory name.

## Database Names

Plugin databases live in the plugin's data directory.

| Plugin Type | Convention | Path |
|-------------|-----------|------|
| Store | `catalog.db` | `~/.local/share/luducat/plugins-data/{name}/catalog.db` |
| Metadata | `enrichment.db` | `~/.local/share/luducat/plugins-data/{name}/enrichment.db` |
| General | `plugin.db` | Via `self.storage.get_db_path()` |

## Keyring Service Names

Credential storage uses a namespaced service name:

```
luducat.{plugin_name}
```

**Examples:** `luducat.steam`, `luducat.gog`, `luducat.epic`, `luducat.igdb`

Declared in `plugin.json`:

```json
{
  "credentials": {
    "use_system_keyring": true,
    "keyring_service": "luducat.my_store"
  }
}
```

## Config Keys

Plugin settings in the global config use the plugin name as a section:

```toml
[plugins.steam]
api_key = "..."
steam_id = "..."

[plugins.my_store]
api_key = "..."
```

## Badge Labels

The `badge_label` in `plugin.json` is a short abbreviation shown in UI badges
(store indicators on game cards).

**Rules:**
- Uppercase
- 3-5 characters
- Recognizable abbreviation of the store name

**Examples:** `"STEAM"`, `"GOG"`, `"EPIC"`

## Brand Colors

The `brand_colors` in `plugin.json` define the badge background and text color:

```json
{
  "brand_colors": {
    "bg": "#1b2838",
    "text": "#66c0f4"
  }
}
```

Use the official brand colors of the store or service. These appear in store
badges and filter UI elements.
