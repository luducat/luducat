# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# runner.py

"""Steam Client Runner

Trivial runner — Steam is always assumed available when the Steam store
plugin is in use. All launches go through the ``steam://rungameid/`` URL
scheme, which is universal across all platforms.
"""

import logging
from pathlib import Path
from typing import List, Optional

from luducat.plugins.base import (
    AbstractRunnerPlugin,
    RunnerLauncherInfo,
    LaunchIntent,
    LaunchMethod,
)

logger = logging.getLogger(__name__)


class SteamRunner(AbstractRunnerPlugin):
    """Runner plugin for Steam Client.

    Steam uses a universal URL scheme for game launching. The Steam client
    is always assumed to be available if the Steam store plugin is active.
    """

    @property
    def runner_name(self) -> str:
        return "steam"

    @property
    def display_name(self) -> str:
        return "Steam Client"

    @property
    def supported_stores(self) -> List[str]:
        return ["steam"]

    def get_launcher_priority(self) -> int:
        return 300

    def detect_launcher(self) -> Optional[RunnerLauncherInfo]:
        """Steam is always considered available.

        The steam:// URL scheme is handled by the OS on all supported
        platforms when Steam is installed.
        """
        return RunnerLauncherInfo(
            runner_name="steam",
            path=None,
            install_type="system",
            virtualized=False,
            url_scheme="steam://",
        )

    def build_launch_intent(
        self, store_name: str, app_id: str
    ) -> Optional[LaunchIntent]:
        """Build steam:// URL scheme launch intent."""
        if store_name != "steam":
            return None

        return LaunchIntent(
            method=LaunchMethod.URL_SCHEME,
            runner_name="steam",
            store_name="steam",
            app_id=app_id,
            url=f"steam://rungameid/{app_id}",
        )

    def build_install_url(self, store_name: str, app_id: str) -> Optional[str]:
        if store_name != "steam":
            return None
        return f"steam://install/{app_id}"
