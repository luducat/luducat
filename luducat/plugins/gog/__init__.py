# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""GOG Store Plugin for luducat

Provides integration with GOG.com game library via:
- GOGdb database import for catalog data
- Heroic Games Launcher (Linux) or GOG Galaxy (Windows) for game launching

Future: GOG API authentication for owned games detection
"""

from .store import GogStore, GogdbImportRequired, GogdbImportRecommended

__all__ = ["GogStore", "GogdbImportRequired", "GogdbImportRecommended"]
