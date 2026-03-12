# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# runner.py

"""Native Direct Launch Runner

Executes native binaries directly. No Wine, no URL schemes — just
``subprocess.Popen`` of a configured executable path.

Never auto-selected (supported_stores is empty, priority 0). Available
only via per-game launch config (Phase G). Foundation for direct game
launch + process tracking + playtime recording.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional

from luducat.plugins.base import (
    AbstractRunnerPlugin,
    RunnerLauncherInfo,
    LaunchIntent,
    LaunchMethod,
)

logger = logging.getLogger(__name__)


class NativeRunner(AbstractRunnerPlugin):
    """Runner plugin for direct native executable launch.

    This runner is manually assigned per-game. It does not auto-detect
    or auto-select — the user must configure the executable path in
    the game's launch settings.
    """

    @property
    def runner_name(self) -> str:
        return "native"

    @property
    def display_name(self) -> str:
        return _("Direct Launch (Native)")

    @property
    def supported_stores(self) -> List[str]:
        return []  # Never auto-selected

    def get_launcher_priority(self) -> int:
        return 0  # Lowest priority — manual assignment only

    def detect_launcher(self) -> Optional[RunnerLauncherInfo]:
        """Always available (native execution capability)."""
        return RunnerLauncherInfo(
            runner_name="native",
            path=None,
            install_type="system",
            virtualized=False,
        )

    def can_launch_game(self, store_name: str, app_id: str) -> bool:
        """Never auto-selected — only via explicit per-game config."""
        return False

    def build_launch_intent(
        self, store_name: str, app_id: str
    ) -> Optional[LaunchIntent]:
        """Build intent from per-game launch config.

        The executable path must come from the per-game launch_config
        (stored in user_game_data). RuntimeManager reads the config
        and passes the executable path to this method via kwargs.
        """
        # Without an executable path, we can't build an intent.
        # RuntimeManager handles injecting the path from per-game config.
        return None

    def build_launch_intent_with_executable(
        self,
        store_name: str,
        app_id: str,
        executable: Path,
        arguments: Optional[List[str]] = None,
        working_directory: Optional[Path] = None,
        environment: Optional[Dict[str, str]] = None,
    ) -> LaunchIntent:
        """Build intent with an explicit executable path.

        Called by RuntimeManager after reading the per-game launch config.
        """
        return LaunchIntent(
            method=LaunchMethod.EXECUTABLE,
            runner_name="native",
            store_name=store_name,
            app_id=app_id,
            executable=executable,
            arguments=arguments or [],
            working_directory=working_directory,
            environment=environment or {},
        )
