# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# wikitext.py

"""PCGamingWiki wikitext template parser.

PCGW disabled arbitrary Cargo queries API-wide (lud-gajy), so game data
now comes from ``action=parse&prop=wikitext`` on individual pages. This
module parses the ``{{Infobox game}}``, ``{{Network/Multiplayer}}`` and
``{{Input}}`` template blocks into dicts with the same display-name keys
the Cargo API returned ("Steam AppID", "Local players", ...), so
database.py and provider.py consume them unchanged.

Parameter-to-field mappings mirror each template's ``#cargo_store``
call (fetched from Template:Infobox_game, Template:Network/Multiplayer
and Template:Input on 2026-08-20).
"""

import re
from typing import Any
from urllib.parse import quote

PCGW_BASE_URL = "https://www.pcgamingwiki.com"

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# {{Network/Multiplayer}} params -> Multiplayer table display keys
_MULTIPLAYER_PARAM_MAP = {
    "local play": "Local",
    "local play players": "Local players",
    "local play modes": "Local modes",
    "lan play": "LAN",
    "lan play players": "LAN players",
    "lan play modes": "LAN modes",
    "online play": "Online",
    "online play players": "Online players",
    "online play modes": "Online modes",
    "asynchronous": "Asynchronous",
    "crossplay": "Crossplay",
    "crossplay platforms": "Crossplay platforms",
}

# {{Input}} params -> Input table display keys
_INPUT_PARAM_MAP = {
    "key remap": "Key remapping",
    "acceleration option": "Mouse acceleration",
    "mouse sensitivity": "Mouse sensitivity",
    "mouse menu": "Mouse input in menus",
    "touchscreen": "Touchscreen",
    "controller support": "Controller support",
    "full controller": "Full controller support",
    "controller remap": "Controller remapping",
    "controller sensitivity": "Controller sensitivity",
    "haptic feedback": "Controller haptic feedback",
}

# {{Infobox game/row/taxonomy/<suffix>}} -> Infobox_game display keys
_TAXONOMY_ROW_MAP = {
    "monetization": "Monetization",
    "microtransactions": "Microtransactions",
    "modes": "Modes",
    "pacing": "Pacing",
    "perspectives": "Perspectives",
    "controls": "Controls",
    "genres": "Genres",
    "sports": "Sports",
    "vehicles": "Vehicles",
    "art styles": "Art styles",
    "themes": "Themes",
    "series": "Series",
}


def _extract_balanced(text: str, start: int) -> str | None:
    """Return the ``{{...}}`` block starting at ``start``, or None if
    the braces never balance."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        pair = text[i:i + 2]
        if pair == "{{":
            depth += 1
            i += 2
            continue
        if pair == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return text[start:i]
            continue
        i += 1
    return None


def extract_template(wikitext: str, name: str) -> str | None:
    """Extract the first ``{{name ...}}`` block from wikitext.

    Matches the template name at a word boundary so "Input" does not
    match "{{Inputxyz" or row templates with a longer path.
    """
    if not isinstance(wikitext, str) or not name:
        return None
    pattern = re.compile(r"\{\{\s*" + re.escape(name) + r"\s*(?=[|}\s])")
    match = pattern.search(wikitext)
    if not match:
        return None
    return _extract_balanced(wikitext, match.start())


def _split_top_level(text: str) -> list[str]:
    """Split on ``|`` that is outside any ``{{...}}`` or ``[[...]]``."""
    parts: list[str] = []
    buf: list[str] = []
    tpl_depth = 0
    link_depth = 0
    i = 0
    n = len(text)
    while i < n:
        pair = text[i:i + 2]
        if pair in ("{{", "}}", "[[", "]]"):
            if pair == "{{":
                tpl_depth += 1
            elif pair == "}}":
                tpl_depth -= 1
            elif pair == "[[":
                link_depth += 1
            else:
                link_depth -= 1
            buf.append(pair)
            i += 2
            continue
        char = text[i]
        if char == "|" and tpl_depth == 0 and link_depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
        i += 1
    parts.append("".join(buf))
    return parts


def parse_template_params(block: str) -> dict[str, str]:
    """Parse named parameters from a ``{{...}}`` template block."""
    if not isinstance(block, str):
        return {}
    inner = block.strip()
    if inner.startswith("{{"):
        inner = inner[2:]
    if inner.endswith("}}"):
        inner = inner[:-2]
    inner = _COMMENT_RE.sub("", inner)
    params: dict[str, str] = {}
    for part in _split_top_level(inner)[1:]:
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        params[key.strip()] = value.strip()
    return params


def _extract_rows(block: str, row_name: str) -> list[list[str]]:
    """Collect positional args of every ``{{row_name|...}}`` in block."""
    rows: list[list[str]] = []
    pattern = re.compile(r"\{\{\s*" + re.escape(row_name) + r"\s*(?=[|}])")
    for match in pattern.finditer(block):
        tpl = _extract_balanced(block, match.start())
        if tpl is None:
            continue
        parts = _split_top_level(tpl[2:-2])
        rows.append([p.strip() for p in parts[1:]])
    return rows


def _join_main_and_side(params: dict[str, str], main: str, side: str) -> str:
    # Mirrors Template:Infobox_game cargo_store:
    # {{{main|}}}{{#if:{{{side|}}}|,{{{side|}}}}}
    main_val = params.get(main, "").strip()
    side_val = params.get(side, "").strip()
    if side_val:
        return f"{main_val},{side_val}"
    return main_val


def parse_infobox(wikitext: str) -> dict[str, Any]:
    """Parse ``{{Infobox game}}`` into Infobox_game display keys."""
    block = extract_template(wikitext, "Infobox game")
    if block is None:
        return {}
    params = parse_template_params(block)
    result: dict[str, Any] = {
        "Steam AppID": _join_main_and_side(
            params, "steam appid", "steam appid side"
        ),
        "GOGcom ID": _join_main_and_side(
            params, "gogcom id", "gogcom id side"
        ),
        "License": params.get("license", "").strip(),
    }

    cover = params.get("cover", "").strip()
    result["Cover URL"] = (
        f"{PCGW_BASE_URL}/wiki/Special:FilePath/{quote(cover)}" if cover else ""
    )

    for suffix, key in _TAXONOMY_ROW_MAP.items():
        rows = _extract_rows(block, f"Infobox game/row/taxonomy/{suffix}")
        if rows and rows[0] and rows[0][0]:
            result[key] = rows[0][0]

    for row_name, key in (
        ("developer", "Developers"),
        ("publisher", "Publishers"),
        ("engine", "Engines"),
    ):
        rows = _extract_rows(block, f"Infobox game/row/{row_name}")
        # First positional arg only; later args are qualifiers (region,
        # engine build) that Cargo does not store either
        values = [r[0] for r in rows if r and r[0]]
        if values:
            result[key] = ", ".join(values)

    for args in _extract_rows(block, "Infobox game/row/date"):
        if len(args) >= 2 and args[0].lower() == "windows":
            result["Released Windows"] = args[1]
            break

    return result


def _parse_mapped_template(
    wikitext: str, template: str, mapping: dict[str, str]
) -> dict[str, str]:
    block = extract_template(wikitext, template)
    if block is None:
        return {}
    params = parse_template_params(block)
    return {
        key: params[param].strip()
        for param, key in mapping.items()
        if param in params
    }


def parse_page(wikitext: str, page_id: Any, page_name: str) -> dict[str, Any]:
    """Parse a PCGW page's wikitext into a Cargo-row-shaped dict.

    Args:
        wikitext: Raw page wikitext from ``action=parse``
        page_id: MediaWiki page id (from the parse response)
        page_name: Page title (from the parse response)

    Returns:
        Dict with the same display-name keys the Cargo API returned.
        Keys from templates absent on the page are omitted, matching a
        cargoquery row where the joined table had no entry.
    """
    if not isinstance(wikitext, str):
        raise TypeError("wikitext must be a string")
    result: dict[str, Any] = {
        "pageID": str(page_id),
        "pageName": str(page_name),
    }
    result.update(parse_infobox(wikitext))
    result.update(
        _parse_mapped_template(
            wikitext, "Network/Multiplayer", _MULTIPLAYER_PARAM_MAP
        )
    )
    result.update(_parse_mapped_template(wikitext, "Input", _INPUT_PARAM_MAP))
    return result
