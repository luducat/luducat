# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# signing.py

"""HMAC-TOTP request signing for IGDB metadata proxy

Generates time-rotating signatures that prove requests come from a luducat
client without identifying individual users. Every installation produces
identical signatures for identical requests at the same time.

The signing key is derived from multiple app constants spread across the
codebase — not a single greppable string.
"""

import hashlib
import hmac
import logging
import time

from luducat.core.constants import APP_ID, APP_VERSION, USER_AGENT_GW

logger = logging.getLogger(__name__)

# Key derived from app ID + fixed key version.
# IMPORTANT: This must match the proxy's KEY_INPUT in auth.ts.
# Do NOT tie this to CONFIG_VERSION or DATABASE_VERSION — those change
# for schema migrations and must not invalidate the signing key.
_SIGNING_KEY_VERSION = 1
_KEY_PARTS = [APP_ID, str(_SIGNING_KEY_VERSION), str(_SIGNING_KEY_VERSION)]
_SIGNING_KEY = hashlib.sha256(":".join(_KEY_PARTS).encode()).digest()

# Signature rotates every 60 seconds (like TOTP)
SIGNATURE_WINDOW = 60

# Default proxy URL (Cloudflare Worker)
IGDB_PROXY_DEFAULT = "https://luducat-api-proxy.luducat-cloudflare.workers.dev"


def sign_request(endpoint: str, body: str) -> str:
    """Generate HMAC-TOTP signature for a proxy request.

    Returns a header value in the format "hex_signature:timestamp_window".
    The proxy validates this by recomputing with the same constants and
    accepting the current window +/- 1 for clock drift tolerance.

    Args:
        endpoint: IGDB API endpoint name (e.g., "games", "covers")
        body: Apicalypse query body

    Returns:
        Signature string for the X-Signature header
    """
    ts_window = str(int(time.time()) // SIGNATURE_WINDOW)
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    message = f"{endpoint}:{ts_window}:{body_hash}"
    sig = hmac.new(_SIGNING_KEY, message.encode(), hashlib.sha256).hexdigest()
    return f"{sig}:{ts_window}"


def get_user_agent() -> str:
    """Build User-Agent string for proxy requests.

    Returns:
        User-Agent header value
    """
    return USER_AGENT_GW
