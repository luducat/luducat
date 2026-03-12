# Community Plugins

Plugins extend luducat with new game stores, metadata sources, platforms,
and launch integrations.

## Submitting a Plugin

There is no formal submission procedure yet. To list your plugin here,
open an issue or pull request.

**Requirements:**
- OSI-approved open source license
- Uses Plugin SDK only (no `luducat.core` imports)
- Includes `plugin.json` with metadata
- See [Plugin SDK Documentation](../plugins/Home.md) and [FAQ](../plugins/FAQ.md)

## Plugin Types

| Type | Description |
|------|-------------|
| Store | Game store integration (library sync, metadata) |
| Metadata | Enrichment data (ratings, game modes, screenshots) |
| Platform | Emulation/compatibility layer (DOSBox, ScummVM, Wine) |
| Runner | Launch delegation to external launchers |

## Plugins

*No community plugins yet. Be the first -- see the [Quickstart](../plugins/quickstart.md).*

### Bundled Plugins (ship with luducat)

| Plugin | Type | Description |
|--------|------|-------------|
| Steam | Store | Steam library, family sharing, VDF sync |
| GOG | Store | GOG library, Galaxy DB, browser auth |
| Epic | Store | Epic library via direct API |
| IGDB | Metadata | Genres, franchises, ratings, media |
| PCGamingWiki | Metadata | Game modes, multiplayer data |
| SteamGridDB | Metadata | Cover art, heroes, logos |
| ProtonDB | Metadata | Linux compatibility ratings |
| Heroic | Metadata | Tag/favourite import from Heroic |
| Lutris | Metadata | Tag/favourite/hidden import from Lutris |
| DOSBox | Platform | DOSBox game detection and configuration |
| ScummVM | Platform | ScummVM game detection and configuration |
| Wine | Platform | Wine/Proton prefix and runtime management |
| Steam Runner | Runner | Launch via Steam client |
| Heroic Runner | Runner | Launch via Heroic |
| Galaxy Runner | Runner | Launch via GOG Galaxy |
| Epic Launcher Runner | Runner | Launch via Epic Games Launcher |
| Lutris Runner | Runner | Launch via Lutris |
| Native Runner | Runner | Direct native binary launch |
