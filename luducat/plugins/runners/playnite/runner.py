# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# runner.py

"""Playnite Runner Plugin

Launches games via the Playnite bridge — a C# plugin inside Playnite that
listens on a local TCP port for IPC commands. The bridge resolves store+ID
to Playnite's internal game GUID and calls PlayniteApi.StartGame().

The runner uses LaunchMethod.IPC with a JSON-over-TLS protocol. Pairing
and session management are handled by bridge_client.py.
"""

import logging
import os
import secrets
from pathlib import Path
from typing import List, Optional

from luducat.plugins.base import (
    AbstractRunnerPlugin,
    RunnerLauncherInfo,
    LaunchIntent,
    LaunchMethod,
    LaunchResult,
)

logger = logging.getLogger(__name__)

# Default bridge port (matches C# BridgeServer default)
DEFAULT_BRIDGE_PORT = 39817


class PlayniteRunner(AbstractRunnerPlugin):
    """Runner plugin that launches games via the Playnite bridge.

    The bridge is a C# Generic Plugin inside Playnite that listens for
    IPC commands on a configurable TCP port. Communication uses JSON over
    TLS with Ed25519 key exchange and HMAC-TOTP session management.

    Priority is deliberately low (50) — Playnite runner is a fallback for
    games that can't be launched via native runners (Steam, Heroic, etc.).
    Users on Windows with Playnite as their primary launcher may want to
    increase this in settings.
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._launcher_info: Optional[RunnerLauncherInfo] = None
        self._detection_done = False
        self._client = None  # Lazy-initialized BridgeClient

    @property
    def runner_name(self) -> str:
        return "playnite"

    @property
    def display_name(self) -> str:
        return "Playnite"

    @property
    def supported_stores(self) -> List[str]:
        return ["steam", "gog", "epic"]

    def get_launcher_priority(self) -> int:
        return 50

    def is_available(self) -> bool:
        """Bridge runners are always available — pairing status is separate."""
        return True

    # ── Detection ─────────────────────────────────────────────────────

    def detect_launcher(self) -> Optional[RunnerLauncherInfo]:
        """Detect bridge availability by checking pairing state.

        Does NOT probe the network on startup — just checks whether we
        have stored pairing credentials. Actual connectivity is verified
        lazily on first launch attempt.
        """
        if self._detection_done:
            return self._launcher_info

        self._detection_done = True

        if not self._is_paired():
            logger.info("Playnite bridge: not paired")
            return None

        host = self.get_setting("bridge_host", "127.0.0.1")
        port = self.get_setting("bridge_port", DEFAULT_BRIDGE_PORT)

        self._launcher_info = RunnerLauncherInfo(
            runner_name="playnite",
            path=None,
            install_type="bridge",
            virtualized=False,
            capabilities={
                "bridge_host": host,
                "bridge_port": port,
                "stores": ["steam", "gog", "epic"],
            },
        )

        logger.info("Playnite bridge: paired, target %s:%d", host, port)
        return self._launcher_info

    # ── Launch ────────────────────────────────────────────────────────

    def build_launch_intent(
        self, store_name: str, app_id: str
    ) -> Optional[LaunchIntent]:
        """Build an IPC launch intent for the Playnite bridge."""
        if store_name not in self.supported_stores:
            return None

        info = self.detect_launcher()
        if not info:
            return None

        return LaunchIntent(
            method=LaunchMethod.IPC,
            runner_name="playnite",
            store_name=store_name,
            app_id=app_id,
            ipc_payload={
                "type": "launch",
                "store": store_name,
                "store_id": app_id,
                "nonce": secrets.token_hex(8),
            },
        )

    def execute_launch(self, intent: LaunchIntent) -> LaunchResult:
        """Execute a launch via the Playnite bridge IPC protocol.

        Connects to the bridge (with HMAC-TOTP auth), sends a launch
        request, and waits for the result.
        """
        if intent.method != LaunchMethod.IPC or not intent.ipc_payload:
            return LaunchResult(
                success=False,
                platform_id="playnite",
                game_id=f"{intent.store_name}/{intent.app_id}",
                error_message="Invalid launch intent for Playnite runner",
            )

        try:
            client = self._get_client()
            if not client.is_connected():
                if not client.connect():
                    return LaunchResult(
                        success=False,
                        platform_id="playnite",
                        game_id=f"{intent.store_name}/{intent.app_id}",
                        error_message=self._bridge_error_message(
                            client, _("connection failed"),
                        ),
                    )
            elif not client.ensure_alive():
                return LaunchResult(
                    success=False,
                    platform_id="playnite",
                    game_id=f"{intent.store_name}/{intent.app_id}",
                    error_message=self._bridge_error_message(
                        client, _("reconnect failed"),
                    ),
                )

            response = client.send_request(intent.ipc_payload)

            if response is None:
                return LaunchResult(
                    success=False,
                    platform_id="playnite",
                    game_id=f"{intent.store_name}/{intent.app_id}",
                    error_message="No response from Playnite bridge",
                )

            if response.get("status") == "ok":
                launch_status = response.get("launch_status", "")
                if launch_status == "started":
                    return LaunchResult(
                        success=True,
                        platform_id="playnite",
                        game_id=f"{intent.store_name}/{intent.app_id}",
                        launch_method=LaunchMethod.IPC,
                    )
                else:
                    return LaunchResult(
                        success=False,
                        platform_id="playnite",
                        game_id=f"{intent.store_name}/{intent.app_id}",
                        error_message=f"Playnite: {launch_status}",
                    )
            else:
                error_msg = response.get("error_message", "Unknown error")
                return LaunchResult(
                    success=False,
                    platform_id="playnite",
                    game_id=f"{intent.store_name}/{intent.app_id}",
                    error_message=f"Playnite bridge: {error_msg}",
                )

        except Exception as e:
            logger.error("Playnite bridge launch failed: %s", e)
            return LaunchResult(
                success=False,
                platform_id="playnite",
                game_id=f"{intent.store_name}/{intent.app_id}",
                error_message=str(e),
            )

    # ── Bridge pairing API (consumed by settings UI) ─────────────────

    @property
    def has_bridge_pairing(self) -> bool:
        return True

    def get_bridge_status(self) -> dict:
        """Return bridge status for the settings panel.

        Returns dict with "status" and optional "detail" keys.
        Status values: not_configured, paired, connected, error.
        """
        if not self._is_paired():
            return {"status": "not_configured"}
        try:
            client = self._get_client()
            if not client.is_connected():
                if client.connect():
                    resp = client.send_request({"type": "ping"})
                    client.disconnect()
                    if resp and resp.get("type") == "pong":
                        return {"status": "connected"}
                return {"status": "paired", "detail": _("not connected")}
            return {"status": "connected"}
        except Exception as e:
            return {"status": "paired", "detail": str(e)}

    def pair_bridge(self, host: str, port: int, on_status,
                    on_code_display=None) -> bool:
        """Run pairing flow.

        Args:
            host: Bridge host address
            port: Bridge TCP port
            on_status: Callback for live status updates: on_status(msg)
            on_code_display: Optional callback(code_str) emitted once the
                6-digit pairing code is available, before blocking on
                complete_pairing().

        Returns True on success.
        """
        from .bridge_client import BridgeClient

        on_status(_("Connecting..."))
        try:
            client = BridgeClient(host=host, port=port, data_dir=self.data_dir)
        except Exception as e:
            on_status(_("Error: {}").format(e))
            return False

        on_status(_("Key exchange..."))
        try:
            code = client.start_pairing()
        except Exception as e:
            on_status(_("Error: {}").format(e))
            return False

        if not code:
            reason = getattr(client, "_last_error", "")
            if reason:
                on_status(_("Error: {}").format(reason))
            else:
                on_status(_("Error: pairing initiation failed"))
            return False

        display = code[:3] + " " + code[3:] if len(code) == 6 else code
        on_status(
            _("Code: {} — enter this in Playnite").format(display)
        )

        if on_code_display:
            on_code_display(code)

        try:
            ok = client.complete_pairing(code, ["launch"])
        except Exception as e:
            on_status(_("Error: {}").format(e))
            return False

        if not ok:
            reason = getattr(client, "_last_error", "")
            if reason:
                on_status(_("Error: {}").format(reason))
            else:
                on_status(_("Error: pairing completion failed"))
            return False

        # Reset lazy state so next detect_launcher picks up new creds
        self._client = None
        self._detection_done = False
        on_status(_("Paired successfully"))
        return True

    def test_bridge_connection(self) -> dict:
        """Test bridge connectivity. Returns status dict like get_bridge_status
        but always attempts a fresh connection+ping."""
        host = self.get_setting("bridge_host", "127.0.0.1")
        port = self.get_setting("bridge_port", DEFAULT_BRIDGE_PORT)

        if not self._is_paired():
            # No local credentials — just check if bridge is reachable
            import socket
            try:
                sock = socket.create_connection((host, port), timeout=5)
                sock.close()
                return {"status": "error", "not_paired": True,
                        "reachable": True,
                        "detail": _("Bridge is running but not paired with this client")}
            except OSError as e:
                return {"status": "error", "not_paired": True,
                        "reachable": False,
                        "detail": str(e)}

        try:
            from .bridge_client import BridgeClient
            client = BridgeClient(host=host, port=port, data_dir=self.data_dir)
            if not client.connect():
                return {"status": "error", "detail": _("Connection failed")}
            resp = client.send_request({"type": "ping"})
            client.disconnect()
            if resp and resp.get("type") == "pong":
                return {"status": "connected"}
            return {"status": "error", "detail": _("No pong response")}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    def unpair_bridge(self) -> None:
        """Clear pairing credentials."""
        if self._client:
            self._client.unpair()
            self._client = None
        else:
            from .bridge_client import BridgeClient
            client = BridgeClient(
                host="127.0.0.1", port=DEFAULT_BRIDGE_PORT,
                data_dir=self.data_dir,
            )
            client.unpair()
        self._detection_done = False
        self._launcher_info = None

    # ── Internal ──────────────────────────────────────────────────────

    @staticmethod
    def _bridge_error_message(client, fallback_reason: str) -> str:
        """Build an actionable error message from bridge client state."""
        host = client.host
        port = client.port
        reason = client.last_error or fallback_reason

        # Try reverse DNS for friendlier display
        display_host = host
        try:
            import socket as _socket
            resolved = _socket.getfqdn(host)
            if resolved and resolved != host:
                display_host = f"{resolved} ({host})"
        except Exception:
            pass

        return _(
            "Could not connect to Playnite bridge at "
            "{host}:{port} ({reason}).\n\n"
            "Is Playnite running on that machine with the "
            "bridge plugin enabled?"
        ).format(host=display_host, port=port, reason=reason)

    def _is_paired(self) -> bool:
        """Check if we have stored pairing credentials."""
        try:
            from luducat.plugins.runners.playnite.bridge_client import BridgeClient
            return BridgeClient.has_stored_credentials(self.data_dir)
        except Exception:
            return False

    def _get_client(self):
        """Get or create the bridge client (lazy init)."""
        if self._client is None:
            from luducat.plugins.runners.playnite.bridge_client import BridgeClient
            host = self.get_setting("bridge_host", "127.0.0.1")
            port = self.get_setting("bridge_port", DEFAULT_BRIDGE_PORT)
            self._client = BridgeClient(
                host=host,
                port=port,
                data_dir=self.data_dir,
            )
        return self._client

    def clear_cache(self) -> None:
        """Reset detection state and disconnect client."""
        self._launcher_info = None
        self._detection_done = False
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
