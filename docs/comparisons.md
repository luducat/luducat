# luducat vs. Other Tools

luducat is a **catalogue browser**, not a launcher or game manager. This page
explains how it fits alongside tools you may already use, and where it differs.

The short version: luducat is not a replacement for Playnite, Heroic, or your
store clients. It is designed to work alongside them.

---

## luducat vs. Playnite

Playnite is a Windows-native game launcher and library manager with a large
plugin ecosystem. luducat and Playnite have overlapping scope in some areas,
but differ significantly in platform, philosophy, and design.

| | luducat | Playnite |
|---|---|---|
| **Primary platform** | Linux (Windows port available) | Windows only |
| **Role** | Catalogue browser | Launcher + library manager |
| **Launch model** | Delegates to existing launchers | Manages and launches directly |
| **Plugin sandbox** | Enforced — plugins declare domains, cannot access outside SDK boundary | None — plugins have full system access |
| **Telemetry control** | Technically enforced, verifiable in UI | Depends on each plugin |
| **Offline mode** | Verifiable in UI (Network Monitor) | Not available |
| **Secrets storage** | System keyring, always encrypted | Varies by plugin |
| **Dark mode** | Automatic, system-aware | Manual |
| **HiDPI / 4K** | Automatic scaling | Limited |
| **Theme switching** | Live, no restart required | Restart required |
| **Metadata fields** | ~22 enriched fields per game | Fewer, plugin-dependent |
| **GOG integration** | Deep (3-tier gap-filler, cookie auth, vertical cover priority) | Plugin-dependent |
| **Plugin enable/disable** | Without restart | Requires restart |
| **Backup system** | Built-in, SHA256 checksummed, configurable retention | Basic |
| **Data formats** | SQLite + TOML (standard, portable) | Proprietary |
| **ProtonDB / Steam Deck badges** | Built-in | Plugin required |
| **Deduplication** | Built-in | Plugin required |
| **CSV export** | Built-in | Plugin required |

**The fundamental difference** is that Playnite's plugin architecture gives
plugins unrestricted access to the system. Any Playnite plugin can read and
write files, make network requests, and execute processes without restriction.
This cannot be fixed without breaking most existing plugins. luducat's plugin
system enforces a strict SDK boundary — plugins declare what domains they need,
and violations result in network access being cut.

Playnite does not have a native Linux version and there are no concrete plans
for one. luducat is Linux-first and the Windows port is a first-class citizen.

A Playnite bridge plugin is planned for luducat, allowing users who run both
to launch games via Playnite from luducat.

---

## luducat vs. Heroic Games Launcher

Heroic is a launcher for GOG and Epic games on Linux. It is not a catalogue
browser.

| | luducat | Heroic |
|---|---|---|
| **Role** | Catalogue browser | Launcher + installer |
| **Installs games** | No | Yes |
| **Manages Wine/Proton** | No | Yes |
| **Multi-store view** | Steam + GOG + Epic unified | GOG + Epic only |
| **Metadata enrichment** | IGDB, PCGamingWiki, SteamGridDB, ProtonDB | Basic store metadata |
| **Offline catalogue** | Full, verifiable | Limited |
| **Deduplication** | Yes | No |
| **Tags / filtering** | Extensive | Basic |

luducat treats Heroic as a launcher — it can hand game launches to Heroic
and import tags and favourites from it. They complement each other.

---

## luducat vs. Steam (Big Picture / Library)

Steam's own library view only shows Steam games. luducat shows Steam alongside
GOG and Epic in one place, with unified metadata and filtering.

luducat also surfaces Steam-specific data that Steam's own UI does not combine:
ProtonDB ratings, Steam Deck compatibility, family sharing status, and hidden /
favourite state — all filterable alongside GOG and Epic games.

---

## luducat vs. GameSieve

[GameSieve](https://github.com/Undeclared-Aubergine/gamesieve) is an early-stage
catalogue tool with similar goals. It is a different project by a different author.
Both tools are open source and not competing — users interested in either are
encouraged to try both.

---

## Summary

luducat fills a specific gap: a privacy-respecting, offline-capable, Linux-first
catalogue that works **alongside** your launchers rather than replacing them. If
you want to install and manage games, use Heroic or your store client. If you want
to browse, filter, and organise everything you own in one place with full metadata
— that is what luducat is for.
