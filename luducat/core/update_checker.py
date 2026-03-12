# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# update_checker.py

"""Update checker for luducat

Checks for new versions via a Cloudflare Workers endpoint.
Opt-in only, privacy-first: sends no data, just fetches a version string.
All HTTP routed through NetworkManager (offline mode, stats, domain check).
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .constants import APP_VERSION, UPDATE_CHECK_URL
from ..utils.signing import sign_request, get_user_agent

logger = logging.getLogger(__name__)


class OfflineError(Exception):
    """Raised when a forced update check is attempted while offline."""


@dataclass
class UpdateInfo:
    """Result of a successful update check."""
    version: str
    changelog: Optional[Dict[str, List[str]]] = field(default=None)


def _parse_version(version_str: str) -> tuple:
    """Parse a version string like '0.2.9.11' or '0.2.9.12a' into a comparable tuple.

    An optional trailing letter suffix (a-z) on the last segment denotes an
    interim checkpoint version.  It sorts after the bare number but before
    the next integer: 0.2.9.12 < 0.2.9.12a < 0.2.9.12b < 0.2.9.13.
    """
    try:
        parts = version_str.strip().split(".")
        result: list = []
        for p in parts:
            # Check for trailing letter suffix (e.g. "12a")
            if p and p[-1].isalpha():
                result.append(int(p[:-1]))
                result.append(p[-1])
            else:
                result.append(int(p))
                # Bare number sorts before any letter suffix:
                # use empty string so  12,"" < 12,"a"
                result.append("")
        return tuple(result)
    except (ValueError, AttributeError):
        return ()


def check_for_update(config, *, force: bool = False) -> Optional[UpdateInfo]:
    """Check if a newer version is available.

    Respects opt-in setting and offline mode. Returns an UpdateInfo
    if an update is available, or None if up-to-date / check skipped /
    error occurred.

    Args:
        config: Config instance
        force: If True, bypass opt-in and dismissed-version guards
               (used for manual "Check Now" button)

    Returns:
        UpdateInfo if update available, None otherwise

    Raises:
        OfflineError: If force=True and app is in offline mode
    """
    # Guard: opt-in check (skip when forced)
    if not force and not config.get("app.check_for_updates", False):
        return None

    # Fetch remote version via NetworkManager (respects offline mode,
    # records stats, enforces domain allowlist)
    from .network_manager import NetworkManager, OfflineError as NmOfflineError

    changelog = None
    try:
        from .network_manager import get_network_manager
        nm = get_network_manager()

        signature = sign_request("version", "")
        resp = nm.execute_request(
            NetworkManager.CORE_PLUGIN,
            "GET",
            UPDATE_CHECK_URL,
            headers={
                "User-Agent": get_user_agent(),
                "X-Signature": signature,
            },
            timeout=10,
        )
        resp.raise_for_status()

        # Try JSON first (new proxy); fall back to plain text (old proxy)
        remote_version = None
        try:
            data = resp.json()
            remote_version = data.get("version", "").strip()
            changelog = data.get("changelog")
        except (ValueError, KeyError, AttributeError):
            remote_version = resp.text.strip()
    except NmOfflineError:
        if force:
            raise OfflineError(
                "Cannot check for updates while in offline mode."
            ) from None
        logger.debug("Update check skipped: offline mode")
        return None
    except Exception as e:
        logger.debug(f"Update check failed: {e}")
        return None

    if not remote_version:
        return None

    # Compare versions
    remote_tuple = _parse_version(remote_version)
    local_tuple = _parse_version(APP_VERSION)

    if not remote_tuple or not local_tuple:
        logger.debug(f"Update check: could not parse versions ({APP_VERSION} vs {remote_version})")
        return None

    if remote_tuple <= local_tuple:
        logger.debug(f"Update check: up to date ({APP_VERSION})")
        return None

    # Check dismissed version (skip when forced)
    if not force:
        dismissed = config.get("app.update_dismissed_version", "")
        if dismissed == remote_version:
            logger.debug(f"Update check: version {remote_version} was dismissed by user")
            return None

    logger.info(f"Update available: {APP_VERSION} -> {remote_version}")
    return UpdateInfo(version=remote_version, changelog=changelog)
