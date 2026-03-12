# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# dt.py

"""UTC datetime helpers and release-date normalisation.

Drop-in replacements for the deprecated ``datetime.utcnow()`` and
``datetime.utcfromtimestamp()`` (removed in Python 3.14).

Returns **naive** datetimes (tzinfo=None) so they remain compatible with
the existing SQLite columns that store naive UTC values.

Also provides ``parse_release_date()`` / ``format_release_date()`` used
by the game cache and UI to normalise the many date formats stores emit
(Steam ``"Oct 21, 2008"``, GOG ``"2008-10-21"``, Epic ISO timestamps,
IGDB ``"2008-10-21"``) into a single ``YYYY-MM-DD`` representation.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return current UTC time as a naive datetime."""
    return datetime.now(UTC).replace(tzinfo=None)


def utc_from_timestamp(ts: float) -> datetime:
    """Convert a Unix timestamp to a naive UTC datetime."""
    return datetime.fromtimestamp(ts, tz=UTC).replace(tzinfo=None)


# ── Release-date helpers ──────────────────────────────────────────────

_NON_DATE_KEYWORDS = frozenset(
    ("coming soon", "to be announced", "tba", "tbd", "early access")
)


def parse_release_date(date_str: str | None) -> str | None:
    """Parse any release-date string to ISO ``YYYY-MM-DD``.

    Fast-path shortcuts handle already-ISO strings and ISO timestamps
    without importing *dateutil*.  Everything else falls through to
    ``dateutil.parser.parse(fuzzy=True)`` which handles virtually every
    human-readable date format the store plugins emit.

    Returns ``None`` for empty strings, known non-date placeholders
    (``"Coming Soon"``, ``"TBA"``, …), and genuinely unparseable input.
    """
    if not date_str:
        return None

    s = date_str.strip()
    if not s:
        return None

    # Fast path — already ISO  (``"2008-10-21"`` or ``"2020-09-22T…"``)
    if len(s) >= 10 and s[4] == "-" and s[:4].isdigit():
        return s[:10]

    # Skip known non-date placeholders
    lower = s.lower()
    if any(kw in lower for kw in _NON_DATE_KEYWORDS):
        return None

    # Robust fallback via dateutil
    try:
        from dateutil.parser import parse as dateutil_parse

        dt = dateutil_parse(s, fuzzy=True, default=datetime(1, 1, 1))
        if dt.year < 1970:
            return None  # failed to extract a meaningful year
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return None


def format_release_date(iso_str: str | None) -> str:
    """Format an ISO ``YYYY-MM-DD`` date for display.

    * ``"2008-10-21"`` → ``"Oct 21, 2008"``
    * ``"2008-01-01"`` → ``"2008"``  (year-only dates stored as Jan 1)
    * Non-ISO / empty → returned as-is (or ``""``).
    """
    if not iso_str or len(iso_str) < 10 or iso_str[4] != "-":
        return iso_str or ""
    try:
        dt = datetime.strptime(iso_str[:10], "%Y-%m-%d")
        if dt.month == 1 and dt.day == 1:
            return str(dt.year)
        return dt.strftime("%b %d, %Y")
    except ValueError:
        return iso_str
