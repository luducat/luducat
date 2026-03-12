"""GameVault Runner -- Demo runner plugin for the luducat Plugin SDK.

This plugin demonstrates a runner that delegates game launching to the
fictional GameVault application via URL schemes or direct execution.

Key concepts demonstrated:
- All required AbstractRunnerPlugin methods
- Launcher detection via sdk.app_finder with caching
- Building structured LaunchIntent objects
- URL scheme vs executable launch methods
- Graceful handling of unsupported stores
- Settings-driven custom launcher path
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


class GameVaultRunner(AbstractRunnerPlugin):
    """GameVault runner plugin.

    Demonstrates launching games through the GameVault desktop application.
    In reality, GameVault doesn't exist -- this is a teaching example.

    Launch methods:
    - LaunchMethod.URL_SCHEME: open a URI (e.g. "gamevault://launch/gamevault/42")
    - LaunchMethod.EXECUTABLE: run the GameVault binary with arguments

    Uses luducat.plugins.sdk.app_finder to detect installed launchers.
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._launcher_info: Optional[RunnerLauncherInfo] = None
        self._detection_done = False

    # ── Required Properties ──────────────────────────────────────────

    @property
    def runner_name(self) -> str:
        return "gamevault_runner"

    @property
    def display_name(self) -> str:
        return "GameVault Runner"

    @property
    def supported_stores(self) -> List[str]:
        """Store plugin names this runner can launch games for."""
        return ["gamevault"]

    # ── Required Methods ─────────────────────────────────────────────

    def detect_launcher(self) -> Optional[RunnerLauncherInfo]:
        """Detect if the GameVault application is installed.

        Returns:
            RunnerLauncherInfo with path, install_type, capabilities,
            or None if the launcher is not found.
        """
        if self._detection_done:
            return self._launcher_info
        self._detection_done = True

        from luducat.plugins.sdk.app_finder import (
            find_application,
            find_url_handler,
        )

        results = find_application(["gamevault"])
        if not results:
            logger.debug("GameVault Runner: launcher not detected")
            return None

        r = results[0]
        url_handler = find_url_handler("gamevault")

        self._launcher_info = RunnerLauncherInfo(
            runner_name=self.runner_name,
            path=r.path,
            install_type=r.install_type,
            virtualized=r.virtualized,
            url_scheme="gamevault://" if url_handler else None,
            capabilities={
                "url_handler_registered": bool(url_handler),
                "stores": self.supported_stores,
            },
        )
        logger.info(
            "GameVault Runner: detected %s install at %s",
            r.install_type,
            r.path,
        )
        return self._launcher_info

    def build_launch_intent(
        self, store_name: str, app_id: str
    ) -> Optional[LaunchIntent]:
        """Build a launch intent for a game.

        Args:
            store_name: luducat store name (e.g. "gamevault").
            app_id: Store-specific app ID string.

        Returns:
            LaunchIntent describing how to launch, or None if
            this runner cannot launch the given game.
        """
        if store_name not in self.supported_stores:
            logger.debug(
                "GameVault Runner: unsupported store %s", store_name
            )
            return None

        info = self.detect_launcher()
        if not info:
            return None

        url = f"gamevault://launch/{store_name}/{app_id}"

        # Prefer URL scheme when handler is registered
        if info.capabilities.get("url_handler_registered"):
            return LaunchIntent(
                method=LaunchMethod.URL_SCHEME,
                runner_name=self.runner_name,
                store_name=store_name,
                app_id=app_id,
                url=url,
            )

        # Fall back to direct binary execution
        if info.path:
            return LaunchIntent(
                method=LaunchMethod.EXECUTABLE,
                runner_name=self.runner_name,
                store_name=store_name,
                app_id=app_id,
                executable=info.path,
                arguments=["--launch", app_id],
            )

        return None

    # ── Optional: Install URL ────────────────────────────────────────

    def build_install_url(
        self, store_name: str, app_id: str
    ) -> Optional[str]:
        """Build an install URL for a game not yet installed."""
        if store_name not in self.supported_stores:
            return None
        return f"gamevault://install/{app_id}"

    # ── Lifecycle ────────────────────────────────────────────────────

    def on_enable(self) -> None:
        info = self.detect_launcher()
        logger.info(
            "GameVault Runner enabled: launcher %s",
            "detected" if info else "not found",
        )

    def close(self) -> None:
        logger.info("GameVault Runner shutting down")

    def clear_cache(self) -> None:
        """Reset detection state for re-scanning."""
        self._launcher_info = None
        self._detection_done = False
