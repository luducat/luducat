# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# lookup.py

"""Three-outcome result for ``get_metadata_for_store_game()``.

The resolver walks the user's priority chain and needs to tell apart
"this source has no record" from "this source could not be asked".
Only the first may be cached as a miss; the second is asked again next
time. A plain ``None`` cannot carry that difference, so plugins answer
with one of::

    Lookup.found(metadata_dict)
    Lookup.not_found("no catalog row")
    Lookup.unavailable("rate_limited")

Legacy ``dict | None`` returns keep working; the resolver wraps them
with :meth:`Lookup.from_legacy` (dict -> found, None -> not_found).

Self-contained: zero imports from ``luducat.core``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


# A catalog miss on the lazy path may cost a request, but never a stall:
# past this pending limiter wait a hop answers unavailable instead.
ON_MISS_MAX_WAIT_SECONDS = 2.0


class LookupOutcome(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Lookup:
    """One hop's answer. Build through the classmethods, not directly."""

    outcome: LookupOutcome
    data: dict[str, Any] | None = None
    reason: str = ""

    @classmethod
    def found(cls, data: dict[str, Any]) -> Lookup:
        if not isinstance(data, dict) or not data:
            raise ValueError("Lookup.found() needs a non-empty metadata dict")
        return cls(LookupOutcome.FOUND, data)

    @classmethod
    def not_found(cls, reason: str = "") -> Lookup:
        return cls(LookupOutcome.NOT_FOUND, None, reason)

    @classmethod
    def unavailable(cls, reason: str) -> Lookup:
        if not reason:
            raise ValueError("Lookup.unavailable() needs a reason")
        return cls(LookupOutcome.UNAVAILABLE, None, reason)

    @classmethod
    def from_legacy(cls, value: Any) -> Lookup:
        """Wrap a legacy ``dict | None`` return; pass a Lookup through."""
        if isinstance(value, Lookup):
            return value
        if value is None:
            return cls.not_found()
        if isinstance(value, dict):
            return cls.found(value) if value else cls.not_found()
        raise TypeError(
            f"lookup returned {type(value).__name__}, expected dict, None or Lookup"
        )

    @property
    def is_found(self) -> bool:
        return self.outcome is LookupOutcome.FOUND


def miss_is_valid(updated_at: datetime | None, ttl: timedelta) -> bool:
    """True when a cached no_match should be honoured (not retried yet).

    Enrichment plugins call this to decide whether a stored miss is
    still within its TTL. A missing timestamp counts as expired so the
    caller retries immediately.
    """
    if updated_at is None:
        return False
    from luducat.plugins.sdk.datetime import utc_now
    return (utc_now() - updated_at) <= ttl
