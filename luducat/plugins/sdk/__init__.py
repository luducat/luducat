# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""luducat Plugin SDK — clean GPL boundary for third-party plugins.

Third-party plugins import ONLY from ``luducat.plugins.base`` (contracts)
and ``luducat.plugins.sdk.*`` (utilities).  No direct ``luducat.core.*``
imports are allowed.

Self-contained modules (json, datetime, text) contain their own
implementation code — zero imports from ``luducat.core``.

Shim modules (network, cookies, config, dialogs) delegate to
implementations injected via ``_registry`` at startup.
"""

SDK_VERSION = "0.1.0"

__all__ = ["SDK_VERSION"]
