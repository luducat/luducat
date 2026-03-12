# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""ProtonDB metadata provider plugin for luducat

Provides Linux/Proton compatibility ratings for Steam games:
- Tier rating (Platinum, Gold, Silver, Bronze, Borked)
- Compatibility score (0.0-1.0)
"""

from .provider import ProtonDbProvider

__all__ = ["ProtonDbProvider"]
