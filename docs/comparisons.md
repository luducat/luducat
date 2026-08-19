# luducat vs. Other Tools

luducat is a **catalogue browser**, not a launcher or game manager. This page
explains how it fits alongside tools you may already use, and where it differs.

The short version: luducat is not a replacement for Playnite, Heroic, or your
store clients. It is designed to work alongside them.

---

## luducat vs. Playnite

Playnite is a Windows-native game launcher and library manager with a large
plugin ecosystem and built-in emulator support. luducat and Playnite have
overlapping scope in some areas but differ significantly in platform,
philosophy, and design.

| | luducat | Playnite |
|---|---|---|
| **Primary platform** | Linux (Windows port available) | Windows only |
| **Linux support** | Native, first-class | None (P11 target: "sometime in 2026," alpha not yet released) |
| **Role** | Catalogue browser | Launcher + library manager |
| **Launch model** | Delegates to existing launchers | Manages and launches directly |
| **Emulator support** | DOSBox, ScummVM (as platform plugins) | Extensive (RetroArch, RPCS3, Dolphin, PCSX2, dozens more) |
| **Plugin sandbox** | Enforced - plugins declare domains, cannot access outside SDK boundary | None - plugins have full system access |
| **Plugin ecosystem** | Bundled store + metadata plugins | 44 library integrations, many community add-ons |
| **Telemetry control** | Technically enforced, verifiable in UI | Depends on each plugin |
| **Offline mode** | Verifiable in UI (Network Monitor) | Not available |
| **Secrets storage** | System keyring, always encrypted | Varies by plugin |
| **Dark mode** | Automatic, system-aware | Manual |
| **HiDPI / 4K** | Automatic scaling | Limited |
| **Theme switching** | Live, no restart required | Restart required |
| **Metadata fields** | ~22 enriched fields per game | Fewer, plugin-dependent |
| **GOG integration** | Deep (3-tier gap-filler, cookie auth, vertical cover priority, offline installer downloader) | Plugin-dependent |
| **Plugin enable/disable** | Without restart | Requires restart |
| **Backup system** | Built-in, SHA256 checksummed, configurable retention | Basic |
| **Data formats** | SQLite + TOML (standard, portable) | Proprietary |
| **ProtonDB / Steam Deck badges** | Built-in | Plugin required |
| **Deduplication** | Built-in | Plugin required |
| **CSV export** | Built-in | Plugin required |
| **Content filter** | Built-in, multi-source scoring | Not built-in |
| **Game mode badges** | Built-in (MP, CO-OP, LAN, MMO, etc.) | Not built-in |
| **Source code hosting** | GitHub | Codeberg (migrated from GitHub) |

**The fundamental difference** is that Playnite's plugin architecture gives
plugins unrestricted access to the system. Any Playnite plugin can read and
write files, make network requests, and execute processes without restriction.
This cannot be fixed without breaking most existing plugins. luducat's plugin
system enforces a strict SDK boundary - plugins declare what domains they need,
and violations result in network access being cut.

Playnite's strongest area is emulation. If you manage a large emulator library,
Playnite handles that well and luducat does not attempt to compete there.

Playnite does not have a native Linux version. Version 11 is a rewrite using
Avalonia UI that is supposed to bring Linux support. As of June 2026, the P11
alpha has not been released and no timeline has been given. Current releases
remain Windows-only.

A luducat Playnite bridge plugin allows users who run both to launch Windows
games from luducat's UI via Playnite over the local network.

---

## luducat vs. Heroic Games Launcher

Heroic is a launcher and installer for GOG, Epic, Amazon, and ZOOM Platform
games on Linux, Windows, and macOS. It focuses on getting games installed and
running.

| | luducat | Heroic |
|---|---|---|
| **Role** | Catalogue browser | Launcher + installer |
| **Installs games** | No (downloads GOG offline installers for archiving) | Yes (GOG, Epic, Amazon, ZOOM) |
| **Manages Wine/Proton** | No (delegates to runners) | Yes (Proton-CachyOS, GE-Proton, GPTK) |
| **Multi-store view** | Steam + GOG + Epic + ZOOM + more, unified | GOG + Epic + Amazon + ZOOM |
| **Steam support** | Full (12,000+ games, family sharing, tag sync) | None |
| **Metadata enrichment** | IGDB, PCGamingWiki, SteamGridDB, ProtonDB | PCGamingWiki, ProtonDB, HowLongToBeat, SteamGridDB |
| **Custom artwork** | SteamGridDB with per-author quality scoring | SteamGridDB (since v2.22) |
| **Offline catalogue** | Full, verifiable | Limited |
| **Deduplication** | Yes (cross-store, normalized titles) | No |
| **Tags / filtering** | Extensive (weighted scoring, user tags, game modes, genres, developers, publishers, release year, store filters, family sharing filter) | Basic (category filters, installed/uninstalled) |
| **Cloud saves** | Not applicable (catalogue, not launcher) | Yes (Epic and GOG) |
| **Achievements** | Not applicable | GOG achievements display (v2.21+) |
| **GOG Deals** | Not applicable | Built-in Deals page with owned/wishlist filters |
| **Console/Big Screen mode** | No | Yes (v2.21+, with install and update support) |
| **GOG offline installer download** | Yes, built-in | No (installs via Galaxy protocol) |
| **Content filter** | Built-in, multi-source scoring | No |
| **Privacy** | No telemetry, no analytics | Plausible analytics (opt-in, off by default) |
| **Backup/restore** | Built-in | No |
| **CSV export** | Built-in | No |

luducat treats Heroic as a launcher - it can hand game launches to Heroic
and import tags and favourites from it. They complement each other: Heroic
installs and runs games, luducat organizes and browses your full library
across all stores.

Heroic is excellent at what it does. If your primary need is installing and
running GOG or Epic games on Linux with Wine/Proton management, Heroic is the
right tool. If you want to see all 14,000 of your games across all stores in
one place with rich metadata and filtering, that is what luducat is for.

---

## luducat vs. GOG Galaxy

GOG Galaxy is GOG's official client. It has been in beta since 2019.

| | luducat | GOG Galaxy |
|---|---|---|
| **Platform** | Linux (native), Windows | Windows, macOS (no Linux) |
| **Status** | Stable releases since March 2026 | Still in beta (v2.0.97, April 2026) |
| **Open source** | Yes (GPLv3) | No (proprietary) |
| **Role** | Catalogue browser | Launcher + store client |
| **Store integration** | Steam, GOG, Epic, ZOOM, and more | GOG native; Xbox and Epic official integrations; Steam, PlayStation, Nintendo via community plugins |
| **Integration reliability** | Direct API access per plugin | Community integrations may need updates after platform API changes |
| **GOG offline installers** | Built-in downloader | Downloads via Galaxy protocol |
| **Metadata** | IGDB, PCGamingWiki, SteamGridDB, ProtonDB | GOG store data only |
| **Cross-store dedup** | Built-in | Manual |
| **Tags / filtering** | Extensive | Basic (Power Search, categories) |
| **Privacy** | No telemetry, all data local | Telemetry present |
| **Plugin sandbox** | Enforced | Not applicable |
| **Overlay** | No | Yes |
| **Achievements** | Not applicable | Yes (GOG games) |
| **Cloud saves** | Not applicable | Yes (GOG games) |
| **Backup/restore** | Built-in | No |
| **Linux client** | Available now | Announced (senior engineer hired), no release date |

GOG Galaxy's cross-platform integration feature allows viewing games from other
stores alongside GOG games. Xbox and Epic integrations are officially maintained
by GOG. Steam, PlayStation, and Nintendo integrations are community-maintained
and may require manual updates after API changes.

luducat's store plugins each access their respective store APIs directly, so
they do not depend on a shared integration layer.

---

## luducat vs. Steam (Big Picture / Library)

Steam's own library view only shows Steam games. luducat shows Steam alongside
GOG, Epic, and other stores in one place, with unified metadata and filtering.

| | luducat | Steam Library |
|---|---|---|
| **Games shown** | All stores, unified | Steam games only |
| **Cross-store view** | Yes | No |
| **Dynamic collections** | Tags with weighted scoring, FilterCrumbs | Store tags and filters |
| **Shelf customization** | Three view modes (list, cover, screenshot) | Customizable shelves with drag-and-drop |
| **Metadata enrichment** | IGDB, PCGamingWiki, SteamGridDB, ProtonDB, cross-store | Steam store data only |
| **ProtonDB ratings** | Integrated, filterable | Not shown |
| **Steam Deck badges** | Integrated, filterable | Shown on store page only |
| **Family sharing filter** | Yes | No |
| **GOG / Epic games** | Full support | Not visible |
| **Remote download** | No | Yes (April 2026) |
| **Community features** | No | Reviews, guides, screenshots, workshop |
| **Game Recording** | No | Built-in |
| **Hardware spec reviews** | No | Yes (March 2026) |
| **Privacy** | No telemetry | Telemetry present (some opt-in) |

luducat surfaces Steam-specific data that Steam's own UI does not combine in
one place: ProtonDB ratings, Steam Deck compatibility, family sharing status,
hidden/favourite state, and game mode information - all filterable alongside
your GOG and Epic games.

Steam's strength is in its community features (workshop, reviews, guides, game
recording) and as a storefront. luducat does not attempt to replicate those. It
shows your Steam library as part of a larger whole.

---

## luducat vs. GameSieve

[GameSieve](https://github.com/Undeclared-Aubergine/gamesieve) is an early-stage
catalogue tool with similar goals. It is a different project by a different author.
Both tools are open source and not competing - users interested in either are
encouraged to try both.

---

## Summary

luducat fills a specific gap: a privacy-respecting, offline-capable, Linux-first
catalogue that works **alongside** your launchers rather than replacing them. If
you want to install and manage games, use Heroic or your store client. If you want
to browse, filter, and organise everything you own in one place with full metadata
- that is what luducat is for.

---

## Changes

**August 2026:** Updated Heroic to v2.22.1 (added ZOOM Platform store, SteamGridDB
integration, updated Wine/Proton runtime names, corrected metadata provider list
to include PCGamingWiki, ProtonDB, HowLongToBeat, and SteamGridDB).

**June 2026:** Rewrote all comparison sections with current data. Added GOG Galaxy
section. Updated Heroic to v2.22 (library editing, GOG achievements, console mode,
analytics). Updated Playnite to v10.56, noted Codeberg migration and that P11 alpha
remains unreleased. Updated Steam with 2026 features (remote downloads, hardware
spec reviews). Expanded comparison tables with content filter, game mode badges,
GOG downloader, and privacy details. Fixed Playnite bridge being listed as planned
(shipped since v0.5.0).
