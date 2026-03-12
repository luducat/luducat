# SDK: Constants (`sdk.constants`)

Application constants and badge/tier color definitions.

## Import

```python
from luducat.plugins.sdk.constants import (
    APP_NAME,
    APP_VERSION,
    USER_AGENT,
    USER_AGENT_GW,
    GAME_MODE_LABELS,
    GAME_MODE_FILTERS,
    PROTONDB_TIER_LABELS,
    PROTONDB_TIER_COLORS,
    STEAM_DECK_LABELS,
    STEAM_DECK_COLORS,
    DEFAULT_TAG_COLOR,
    TAG_SOURCE_COLORS,
)
```

## Application Constants

| Constant | Type | Description |
|----------|------|-------------|
| `APP_NAME` | `str` | Application name (`"luducat"`) |
| `APP_VERSION` | `str` | Current version (`"0.2.9.28"`) |
| `USER_AGENT` | `str` | Browser-like User-Agent for web requests |
| `USER_AGENT_GW` | `str` | App-identifying User-Agent for luducat API proxy |

Use `USER_AGENT` for requests to third-party APIs:

```python
self.http.get(url, headers={"User-Agent": USER_AGENT})
```

## Badge Constants

### Game Mode Labels

`GAME_MODE_LABELS: Dict[str, str]` -- maps game mode keys to display labels:
MP, CO-OP, LOCAL, L-COOP, L-VS, O-VS, PVP, LAN, MMO, BR.

### ProtonDB Tiers

`PROTONDB_TIER_LABELS: Dict[str, str]` -- maps tier keys to display labels.
`PROTONDB_TIER_COLORS: Dict[str, str]` -- maps tier keys to hex colors.

### Steam Deck Compatibility

`STEAM_DECK_LABELS: Dict[str, str]` -- maps compatibility keys to labels.
`STEAM_DECK_COLORS: Dict[str, str]` -- maps compatibility keys to hex colors.

### Tag Colors

`DEFAULT_TAG_COLOR: str` -- default hex color for user tags.
`TAG_SOURCE_COLORS: Dict[str, str]` -- maps tag source names to brand colors.

## Gotchas

- **Version checking.** Use `APP_VERSION` if you need to log which luducat
  version your plugin is running under. Don't use it for feature detection --
  use `min_luducat_version` in `plugin.json` instead.
