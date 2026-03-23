# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# news.py

"""User-facing changelog/news for luducat

This module contains user-friendly release notes displayed in the News tab
of the About dialog. Entries should be written in plain English, focusing
on what users will notice rather than technical implementation details.

When bumping APP_VERSION, add a new entry at the top of NEWS_ENTRIES
and a corresponding entry in UPDATE_SUMMARIES.
"""

from typing import Any, Dict, List, Optional

# News entries - newest first
# Format: list of dicts with version, date, and items
# Each item has optional "section" prefix and required "text"
NEWS_ENTRIES: List[Dict[str, Any]] = [
    {
        "version": "0.6.0",
        "date": "2026-03-23",
        "items": [
            {"section": "Store Engine", "text":
             "Declarative store plugin system for adding new DRM-free stores "
             "via JSON rulesets. Ships with three new stores: ZOOM Platform, "
             "JAST USA, and MangaGamer."},
            {"section": "Badges", "text":
             "SVG icon badges replace text store pills in all views. "
             "Corner triangle badges for game modes scale with cover size."},
            {"section": "", "text": "Demo detection separated from free-to-play games"},
            {"section": "", "text": "Content filter keyword scoring for better adult content detection"},
            {"section": "", "text": "Badge visibility toggles in Settings"},
            {"section": "", "text": "Updated translations for all languages"},
        ],
    },
    {
        "version": "0.5.1",
        "date": "2026-03-16",
        "items": [
            {"section": "Playnite Bridge", "text":
             "Enables remote launching of Playnite-managed games from luducat over "
             "the local network. Browse your catalogue on Linux (or a second Windows "
             "machine) and launch games on the Windows PC where Playnite is running. "
             "Requires installing the Luducat Bridge plugin in Playnite — a one-time "
             "setup that pairs both sides securely. Combined with Sunshine and "
             "Moonlight, you can stream the game back to your screen for a seamless "
             "couch gaming experience."},
            {"section": "", "text": "Updated translations for German, French, Spanish, and Italian"},
            {"section": "", "text": "Documentation and packaging improvements"},
        ],
    },
    {
        "version": "0.5.0",
        "date": "2026-03-12",
        "items": [
            {"section": "", "text": "First public release"},
            {"section": "", "text": "Browse and organize games from Steam, GOG, and Epic in one place"},
            {"section": "", "text": "Metadata enrichment via IGDB, SteamGridDB, and PCGamingWiki"},
            {"section": "", "text": "Tag system, filters, sorting, and multiple view modes"},
            {"section": "", "text": "Theme support with 14 bundled themes"},
            {"section": "", "text": "Plugin SDK for third-party extensions"},
        ],
    },
]


def get_news_html() -> str:
    """Build HTML content from news entries for display in QTextBrowser.

    Returns:
        HTML string with formatted news entries
    """
    html_parts = ['<html><body>']

    for entry in NEWS_ENTRIES:
        version = entry["version"]
        date = entry["date"]
        items = entry["items"]

        html_parts.append(f'<h3>Version {version} <span style="font-weight: normal;">({date})</span></h3>')
        html_parts.append('<ul>')

        for item in items:
            section = item.get("section", "")
            text = item["text"]
            if section:
                html_parts.append(f'<li><b>{section}:</b> {text}</li>')
            else:
                html_parts.append(f'<li>{text}</li>')

        html_parts.append('</ul>')

    html_parts.append('</body></html>')
    return '\n'.join(html_parts)


# Curated update summaries — terse, 3-category, written during version bump.
# Keys match version strings in NEWS_ENTRIES. Only the latest entry matters
# for the proxy endpoint; older entries are kept for reference.
#
# Categories:
#   "new"      — New UX features, new stores/plugins, non-obvious additions
#   "improved" — Enhancements to things that already existed
#   "fixed"    — User-impacting bugfixes
UPDATE_SUMMARIES: Dict[str, Dict[str, List[str]]] = {
    "0.6.0": {
        "new": [
            "Declarative store engine with ZOOM, JAST USA, and MangaGamer",
            "SVG icon badges in all views",
            "Demo game detection",
        ],
        "improved": [
            "Content filter keyword scoring",
            "Badge density and visibility controls",
            "Translation updates",
        ],
        "fixed": [
            "Store engine metadata re-enrichment loop",
            "Badge overflow on small covers",
        ],
    },
    "0.5.1": {
        "new": [
            "Playnite bridge for remote game launching",
        ],
        "improved": [
            "Translation updates",
            "Documentation and packaging",
        ],
    },
    "0.5.0": {
        "new": [
            "First public release",
        ],
    },
}

_SUMMARY_LABELS = [("new", "New"), ("improved", "Improved"), ("fixed", "Fixed")]


def get_update_summary(version: str) -> Optional[Dict[str, List[str]]]:
    """Get the curated update summary for a specific version."""
    return UPDATE_SUMMARIES.get(version)


def format_summary_text(summary: Dict[str, List[str]]) -> str:
    """Format a summary dict into plain text for tooltips.

    Skips empty categories.
    """
    parts: List[str] = []
    for key, label in _SUMMARY_LABELS:
        items = summary.get(key, [])
        if items:
            parts.append(f"{label}:")
            for item in items:
                parts.append(f"  \u2022 {item}")
    return "\n".join(parts)


def format_summary_html(summary: Dict[str, List[str]]) -> str:
    """Format a summary dict into HTML for scrollable display.

    Skips empty categories.
    """
    html: List[str] = []
    for key, label in _SUMMARY_LABELS:
        items = summary.get(key, [])
        if items:
            html.append(f"<b>{label}:</b><ul>")
            for item in items:
                html.append(f"<li>{item}</li>")
            html.append("</ul>")
    return "\n".join(html)
