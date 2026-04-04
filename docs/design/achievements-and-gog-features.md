# Achievements + GOG Features — Release 1.0

Date: 2026-03-30

## Context

Competitive pressure from Theophany (Rust/QML, planning GOG+Comet integration).
luducat shifts from "catalogue browser" to "catalogue browser that fills the gaps
stores left on Linux." GOG is the biggest gap: no Galaxy on Linux, no achievements,
no cloud saves. Steam has everything native. Epic has Heroic. GOG users have nothing.

Release 0.7.0 ships current dev work. Release 1.0 is the achievements release with
partial GOG Galaxy replacement functionality. Still delegates launching (Heroic/Galaxy),
but owns the achievement and download data layer.

No Comet as subprocess. All data via direct HTTP APIs, Python-native.

## Scope

### In scope (1.0)
1. Generic achievement base (schema, pipeline, UI)
2. GOG achievements via Gameplay API (direct HTTP, per-game OAuth)
3. Steam achievements via Steam Web API
4. Basic GOG game downloads via mget (chunked, resumable)
5. Wine/UMU launch polish

### Designed for but deferred
- Cross-game achievement statistics (completion %, platinum, leaderboards) → 1.1+
- Cloud save sync (GOG cloud protocol undocumented) → needs reverse engineering
- Local save backup (Ludusavi integration) → 1.1+
- RetroAchievements, Epic achievements → future sources
- Install automation (running downloaded GOG installers) → 1.1+ / archivist

## Architecture

### Data Source Strategy

No Comet dependency. No subprocess for data retrieval.

| Source | Auth | Data | Use |
|--------|------|------|-----|
| GOG Gameplay API | Per-game OAuth token | Achievements + unlock status | Primary for GOG |
| Steam Web API | API key + Steam ID | Achievements + unlock status | Primary for Steam |
| GOG Galaxy DB | File read (Windows) | 61k achievements, cloud save paths | Optional bulk import |
| GOGdb | Public (no auth) | Per-game clientId/clientSecret | Token exchange metadata |

### Achievement Data Model

Achievement data is a DETAIL field. Never in GameEntry. Same pattern as IGDB
enrichment — definitions cached in plugin DBs, loaded on demand.

#### Plugin DB Tables (per-store plugin)

```sql
-- Achievement definitions (rarely change)
CREATE TABLE achievement_definitions (
    app_id TEXT NOT NULL,
    achievement_id TEXT NOT NULL,
    name TEXT,
    description TEXT,
    icon_locked_url TEXT,
    icon_unlocked_url TEXT,
    rarity REAL,                 -- 0.0-100.0 global unlock percentage
    rarity_tier TEXT,            -- common/uncommon/rare/epic/legendary
    is_hidden BOOLEAN DEFAULT 0,
    sort_order INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (app_id, achievement_id)
);

-- User unlock status
CREATE TABLE achievement_unlocks (
    app_id TEXT NOT NULL,
    achievement_id TEXT NOT NULL,
    unlocked BOOLEAN DEFAULT 0,
    unlock_time TEXT,            -- ISO timestamp, nullable
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (app_id, achievement_id)
);

-- Per-game summary (for list view badges, cross-game queries)
CREATE TABLE achievement_summary (
    app_id TEXT NOT NULL PRIMARY KEY,
    total INTEGER NOT NULL DEFAULT 0,
    earned INTEGER NOT NULL DEFAULT 0,
    completion_pct REAL DEFAULT 0.0,
    last_unlock_time TEXT,
    fetched_at TEXT NOT NULL
);
```

The `achievement_summary` table enables future cross-game statistics (platinum
completion, leaderboards, top-list queries) without loading per-achievement data.

### Caching Strategy

| Data | TTL | Refresh trigger |
|------|-----|-----------------|
| Definitions (names, icons) | 30 days | Background sync or manual |
| Unlock status | 24 hours | Detail view open, manual |
| Rarity percentages | 7 days | Background sync |
| Per-game clientId/clientSecret | 90 days | Plugin DB |
| Achievement summary | Recomputed on unlock refresh | |

Fetch patterns:
- **Bulk sync**: During store sync, optionally fetch definitions for owned games
  (rate-limited, background, skip if fresh)
- **On-demand**: Opening detail view checks cache TTL. Stale → fetch that game.
- **Unlock refresh**: Opening detail view checks unlock TTL. Stale → re-fetch.

### Memory Strategy

- Zero in-memory cache for achievement details
- Plugin DB queries on demand (raw SQL, no ORM)
- `achievement_summary.completion_pct` stored in game's `metadata_json` for list
  view badge display (computed during sync, one float per game)
- No GameEntry field for per-achievement data
- 15k games × 1 float (completion %) = negligible memory impact

## GOG Achievement Auth Flow

### Token Chain

```
Galaxy refresh token (from Heroic config or browser OAuth)
  → Per-game token exchange (clientId/clientSecret from GOGdb)
    → Per-game access token (3600s TTL)
      → GET gameplay.gog.com/clients/{client_id}/users/{user_id}/achievements
```

### Galaxy Refresh Token Sources

1. **Heroic config** (primary, Linux):
   `~/.config/heroic/gog_store/auth.json` → `refresh_token`
   Heroic Flatpak: `~/.var/app/com.heroicgameslauncher.hgl/config/heroic/gog_store/auth.json`

2. **Browser OAuth flow** (fallback):
   luducat opens `auth.gog.com/auth?client_id=46899977096215655&redirect_uri=...&response_type=code`
   → User logs in → redirect with code → exchange for refresh token
   → Store in keyring via CredentialManager

### Per-Game Credentials

From GOGdb (public, no auth):
1. `GET gogdb.org/data/products/{productId}/product.json` → `builds[]`
2. Pick latest build → `GET gogdb.org/data/products/{productId}/builds/{buildId}.json`
3. Response: `clientId`, `clientSecret`
4. Cache in plugin DB (90-day TTL)

### Token Exchange

```
GET auth.gog.com/token
  ?grant_type=refresh_token
  &refresh_token={galaxy_refresh_token}
  &client_id={per_game_client_id}
  &client_secret={per_game_client_secret}
  &without_new_session=1
→ {access_token, refresh_token, expires_in: 3600, user_id}
```

Per-game access tokens: in-memory dict, 3500s TTL (refresh before expiry).
Updated refresh tokens persisted to keyring.

### Gameplay API Endpoints

```
GET gameplay.gog.com/clients/{client_id}/users/{user_id}/achievements
Authorization: Bearer {per_game_access_token}
→ list of achievements with unlock status, dates, visibility
```

## Steam Achievement Integration

### Endpoints

```
GET ISteamUserStats/GetSchemaForGame/v2
  ?appid={appid}&key={apikey}
→ achievement definitions (name, displayName, description, icon, icongray)

GET ISteamUserStats/GetPlayerAchievements/v1
  ?appid={appid}&steamid={steamid}&key={apikey}
→ per-game unlock status (achieved, unlocktime)

GET ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2
  ?gameid={appid}
→ global unlock percentages (for rarity)
```

### What Exists Already

- Steam API key (user-configured)
- Steam user ID (from family sharing)
- Rate limiter (steamscraper)
- Plugin DB infrastructure

### What Needs Adding

- `get_achievements(appid)` method on Steam API client
- `get_achievement_schema(appid)` for definitions
- `get_global_achievement_percentages(appid)` for rarity
- Storage in steam plugin DB (same generic tables)
- Rarity tier computation from percentages

### Rarity Tier Mapping

| Percentage | Tier | Color |
|------------|------|-------|
| > 50% | common | grey |
| 20-50% | uncommon | green |
| 5-20% | rare | blue |
| 1-5% | epic | purple |
| < 1% | legendary | gold |

Matches GOG Galaxy's tier names. Thresholds configurable in constants.

## Generic Achievement Pipeline

```
Store Plugin (steam/gog)
  → fetch definitions + unlock status via store-specific API
  → store in plugin DB (generic schema)
  → MetadataResolver queries plugin for achievements (existing field)
  → game_service returns achievement list for detail view
  → UI displays in Achievements tab
```

### Plugin Base Changes

- New optional method: `get_achievements(app_id) -> list[dict]`
- New optional method: `sync_achievements(app_ids: list) -> None`
- Achievement capability in plugin.json: `"achievements": true`
- MetadataResolver handles `achievements` field through existing priority system

### Build Order

1. **Steam achievements** — quick win, proves the pipeline. Well-documented API,
   all auth already in place.
2. **Generic achievement UI** — detail view tab, progress bar, rarity badges.
3. **GOG achievements** — Gameplay API integration, token exchange, Heroic token reading.
4. **Achievement summary** — completion % in metadata_json, optional list view badge.

## Detail View UI

### Achievements Tab

New tab between "Stats" and "Files" in the detail view.

**Layout:**
- Header: progress bar (earned/total), completion percentage, last unlock date
- Achievement list: scrollable, each row shows:
  - Icon (unlocked or locked variant)
  - Name + description
  - Rarity badge (tier color + percentage)
  - Unlock time (if unlocked)
  - Hidden achievements: "Hidden achievement" when locked, reveal on unlock

**Theming:**
- Rarity colors via theme variables (5 tier colors)
- Achievement icons: 60×60px (GOG CDN format: `_gac_60`)
- Progress bar: existing StripedProgressBar or simple QProgressBar

### List/Grid View Badge (Optional)

Small achievement badge in list/cover/screenshot views showing completion %
or "100%" platinum indicator. Uses `achievement_summary.completion_pct` from
metadata_json. Low priority — detail view is primary display.

## Basic GOG Downloads

### Data Source

```
GET api.gog.com/products/{id}?expand=downloads
→ downloads.installers[].files[].downlink
```

The `downlink` URL redirects to a CDN download link. Auth via GOG session cookies
(existing BrowserCookieManager flow).

### Download Engine: mget

Use `~/bin/mget/` (own project) as the download engine:
- `download_one()` from `mget_lib/download.py`
- Chunked parallel downloads with resume support
- Cookie jar support for GOG auth
- Progress callbacks for UI integration

Integration: import mget_lib directly (shared venv) or shell out to mget.py.

### Download Flow

1. User clicks "Download" in GOG game detail view
2. Fetch installer list from Products API
3. Show installer picker dialog (platform, language, version)
4. Download via mget to configured download directory
5. Progress in UI (SyncWidget-style or status bar)
6. Track in plugin DB: path, size, version, download date

### Drop Target Window ("GOG Downloader Revival")

Standalone floating window (or dockable panel) that accepts drag-and-dropped GOG
download URLs from the browser. Spiritual successor to the old GOG Downloader
standalone app that GOG killed when they pushed Galaxy.

#### The original GOG Downloader (reference, v3.3.5.0)

Reference screenshot: `~/bin/luducat/luducat-general-dev-notes/design/gogdownloader-picture.jpg`

Small Windows-only standalone app. GOG removed it (404) to push Galaxy adoption.

- Download queue with game cover thumbnails and per-download progress bars
- Resume All / Pause All / per-item cancel
- INSTALL button appeared on each completed download
- Game update notifications badge ("41 NEW — 41 game updates")
- Settings: concurrent connections (up to 6), download speed limit with
  scheduled throttling (time range), custom save directory, auto-run
  installer after download, start on Windows startup
- Online/offline status with connection speed display
- Simple, single-purpose, no bloat

#### Our revival

- User drags URL from GOG download page onto the drop target
- Domain allowlist: only accept URLs from `gog.com` / `*.gog.com`
- luducat resolves the URL to CDN download link via Products API
- mget downloads with progress display
- Works independently of the library view — don't need to own the game in luducat

Two download modes in 1.0:
1. **Library download** — browse catalogue, click Download, pick installer
2. **Drop target** — drag URLs from browser, they just download

#### Update notifications

- On sync, compare installed installer versions against GOG's current version
- Badge/notification when updates are available (like the original's "41 NEW")
- Update dialog: checklist of available updates with game title, current vs new
  version, installer size — user ticks which to download, hits "Queue Selected"
- Queued updates feed into the same download queue as new downloads
- Option to auto-check for updates on sync (Settings toggle)

The drop target is the emotional hook for the r/gog audience. "Remember the GOG
Downloader? We brought it back."

#### First-Run Wizard: "GOG Downloader" shortcut route

For users coming from r/gog who just want a downloader replacement, add a fast-track
path in the first-run wizard. One choice on the wizard's opening page:

- **"Set up as GOG Downloader"** — skips the full wizard, applies this preset:
  - Only GOG store enabled (others can be enabled later)
  - Default download directory configured (prompt once)
  - Auto-check for game updates on sync enabled
  - On startup: open the downloader window, start main window minimized

The main window is always fully functional and accessible — it just starts minimized
so the downloader window is front and center. The user can restore it anytime from
the taskbar. "Start minimized" is a checkbox in Settings (General), not a hidden mode.

This is a **preset, not a mode**. All settings remain individually changeable in
Settings afterward. The user can enable Steam/Epic later, uncheck "start minimized",
etc. No locked-down state, no special code paths — just sensible defaults for the
downloader use case.

The downloader window is a child of the main window (modeless QDialog, same as
Collection Manager). Closing the main window closes the downloader. No independent
process, no tray-only mode.

**Design note:** Once users restore the main window and see their GOG games catalogued
with covers, metadata, and filtering, they'll naturally explore the full experience.
A subtle hint in the downloader window ("Browse your full library →") helps with
discovery.

### Not in 1.0

- No install automation (don't run the installer)
- No DLC download management

## Wine / UMU Launch Polish

Existing Wine subsystem + UMU support. Focus for 1.0:

- UMU-run as preferred Wine/Proton runner (already in Wine provider)
- Smoother GOG game launch via Wine (detect → configure → launch)
- Still delegate to Heroic/Galaxy as primary GOG launchers
- Wine fallback for users without Heroic

Out of scope for 1.0: full Wine prefix management, automatic game detection.

## File Changes (Estimated)

| Area | Files |
|------|-------|
| Achievement schema | Plugin DB migrations (steam, gog) |
| Steam achievements | `plugins/steam/` — API methods, sync |
| GOG achievements | `plugins/gog/` — Gameplay API, token exchange, Heroic token reader |
| Generic pipeline | `plugins/base.py` — achievement methods, `core/metadata_resolver.py` |
| Detail view UI | `ui/list_view.py` — Achievements tab |
| GOG downloads | `plugins/gog/` — installer URL resolution, mget integration |
| Download UI | `ui/` — download dialog/progress |

## Verification

1. Steam achievements display for a game with known achievements
2. GOG achievements display (with Heroic refresh token)
3. Achievement rarity badges match expected tiers
4. Hidden achievements properly hidden when locked
5. Cache TTL respected (no redundant API calls)
6. GOG installer download completes with resume (kill + restart)
7. Full test suite passes
