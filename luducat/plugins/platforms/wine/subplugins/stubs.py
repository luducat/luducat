# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# stubs.py

"""Wine Sub-Plugin Stubs

Structural stubs for post-release features. Each stub is available for
registration but contributes nothing to the launch pipeline.
"""

import shutil
from typing import List

from . import SubPluginType, WineSubPlugin


class ManagedPrefixProvider(WineSubPlugin):
    """Stub: managed prefix creation (post-release)."""

    name = "managed_prefix"
    display_name = "Managed Prefixes"
    sub_type = SubPluginType.PREFIX_PROVIDER
    status = "stub"


class DXVKEnhancement(WineSubPlugin):
    """Stub: DXVK/vkd3d DLL injection (post-release)."""

    name = "dxvk"
    display_name = "DXVK"
    sub_type = SubPluginType.ENHANCEMENT
    status = "stub"


class GamescopeOverlay(WineSubPlugin):
    """Stub: gamescope compositor wrapper (post-release)."""

    name = "gamescope"
    display_name = "Gamescope"
    sub_type = SubPluginType.OVERLAY
    status = "stub"

    @property
    def is_available(self) -> bool:
        return shutil.which("gamescope") is not None

    def contribute_command_prefix(self, cmd: List[str], **kwargs) -> List[str]:
        return cmd  # Stub — no-op


class MangohudOverlay(WineSubPlugin):
    """Stub: MangoHud overlay wrapper (post-release)."""

    name = "mangohud"
    display_name = "MangoHud"
    sub_type = SubPluginType.OVERLAY
    status = "stub"

    @property
    def is_available(self) -> bool:
        return shutil.which("mangohud") is not None

    def contribute_command_prefix(self, cmd: List[str], **kwargs) -> List[str]:
        return cmd  # Stub — no-op
