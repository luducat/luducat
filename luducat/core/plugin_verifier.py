# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# plugin_verifier.py

"""Plugin integrity verification for luducat

Implements the pre-release security layer (I.3a-f):
  a) Merkle fingerprinting of plugin directories
  b) Keyring-based trust anchor storage
  c) Startup verification against known-good hashes
  d) Disable + warn on fingerprint mismatch
  e) Trust state logging (startup/shutdown blocks)
  f) Distribution format detection

See docs/security-model.md for the full design.
"""

import hashlib
import hmac
import logging
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import APP_NAME, APP_VERSION

logger = logging.getLogger(__name__)

# Keyring service and key for trust anchor
TRUST_SERVICE = f"{APP_NAME}.app-state"
TRUST_KEY = "plugin-trust"           # Legacy (v1): full JSON in keyring
TRUST_HMAC_KEY = "trust-hmac-key"    # v2: HMAC secret in keyring (64 hex chars)
TRUST_FILE_NAME = "trust-state.json" # v2: trust data + HMAC on disk

# Trust schema version (increment on trust data structure changes)
TRUST_SCHEMA_VERSION = 1

# Files to include in fingerprint computation
_FINGERPRINT_EXTENSIONS = frozenset({".py"})
_FINGERPRINT_FILENAMES = frozenset({"plugin.json"})

# Directories to exclude from fingerprint scan
_EXCLUDED_DIRS = frozenset({"__pycache__", ".git", ".mypy_cache", ".pytest_cache"})


# --- Trust States ---

class TrustState:
    """Plugin trust verification states."""
    VERIFIED = "verified"           # Fingerprint matches known-good
    UNVERIFIED = "unverified"       # No stored hash (first run, new plugin)
    MISMATCH = "mismatch"           # Fingerprint doesn't match stored hash
    SOURCE_TREE = "source_tree"     # Loaded from source tree (dev mode)
    USER_DISABLED = "user_disabled" # User manually disabled


class TrustTier:
    """Plugin trust tiers."""
    BUNDLED = "bundled"             # Shipped with luducat
    TRUSTED = "trusted"            # Third-party, user-approved
    UNTRUSTED = "untrusted"        # Third-party, not yet approved


# --- I.3a: Merkle Fingerprinting ---

def compute_plugin_fingerprint(plugin_dir: Path) -> Optional[str]:
    """Compute a deterministic Merkle fingerprint for a plugin directory.

    Algorithm:
      1. Collect all .py files and plugin.json
      2. Sort by relative path (deterministic ordering)
      3. SHA-256 each file individually
      4. Concatenate: "relative/path.py:sha256hex\\n" for each file
      5. SHA-256 the concatenation to produce the fingerprint

    Args:
        plugin_dir: Path to the plugin directory

    Returns:
        Hex-encoded SHA-256 fingerprint, or None if directory is invalid
    """
    if not plugin_dir.is_dir():
        return None

    # Collect files to hash
    files: List[Path] = []
    for item in _walk_plugin_dir(plugin_dir):
        rel = item.relative_to(plugin_dir)
        name = rel.name

        if name in _FINGERPRINT_FILENAMES:
            files.append(item)
        elif item.suffix in _FINGERPRINT_EXTENSIONS:
            files.append(item)

    if not files:
        return None

    # Sort by relative path (forward slashes for cross-platform determinism)
    files.sort(key=lambda p: p.relative_to(plugin_dir).as_posix())

    # Build the Merkle concatenation
    parts: List[str] = []
    for filepath in files:
        try:
            file_hash = _hash_file(filepath)
            rel_posix = filepath.relative_to(plugin_dir).as_posix()
            parts.append(f"{rel_posix}:{file_hash}\n")
        except (OSError, IOError) as e:
            logger.warning("Cannot hash %s: %s", filepath, e)
            return None

    # Final hash of the concatenation
    concat = "".join(parts)
    return hashlib.sha256(concat.encode("utf-8")).hexdigest()


def _walk_plugin_dir(plugin_dir: Path):
    """Walk a plugin directory, yielding files (excluding __pycache__ etc.)."""
    for item in plugin_dir.rglob("*"):
        if item.is_file():
            # Check none of the parent dirs are excluded
            skip = False
            for parent in item.relative_to(plugin_dir).parents:
                if parent.name in _EXCLUDED_DIRS:
                    skip = True
                    break
            if not skip:
                yield item


def _hash_file(filepath: Path) -> str:
    """SHA-256 hash a single file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# --- I.3b: Keyring Trust Anchor ---

class TrustStore:
    """Manages plugin trust data with HMAC-verified file storage.

    Architecture (v2):
      - Trust data lives in a JSON file (trust-state.json) in the data dir
      - A 32-byte HMAC key is stored in the system keyring (always fits,
        even on Windows Credential Manager's 2560-byte limit)
      - The file contains both the trust data and its HMAC signature
      - On load, the HMAC is verified against the keyring-stored key
      - Filesystem-only tampering is detected because the attacker cannot
        forge the HMAC without access to the keyring

    Migration: If legacy keyring-only data exists (v1), it is migrated
    to the new file+HMAC format automatically.

    Falls back to unsigned file storage if keyring is completely
    unavailable (logged as a warning).
    """

    def __init__(self, credential_manager, data_dir: Optional[Path] = None):
        """Initialize with an existing CredentialManager instance.

        Args:
            credential_manager: The application's CredentialManager
            data_dir: Directory for trust-state.json (defaults to
                      platformdirs user_data_dir)
        """
        self._creds = credential_manager
        self._trust_data: Optional[Dict[str, Any]] = None

        if data_dir is None:
            import platformdirs
            data_dir = Path(platformdirs.user_data_dir(APP_NAME))
        self._trust_file = data_dir / TRUST_FILE_NAME

    # ── HMAC helpers ──────────────────────────────────────────────

    def _get_or_create_hmac_key(self) -> Optional[str]:
        """Get the HMAC key from keyring, creating one if needed.

        Returns:
            Hex-encoded 32-byte key, or None if keyring unavailable
        """
        key = self._creds.get("app-state", TRUST_HMAC_KEY)
        if key and len(key) == 64:
            return key

        # Generate new key
        key = secrets.token_hex(32)
        if self._creds.store("app-state", TRUST_HMAC_KEY, key):
            # Verify it actually persisted to keyring (not just file fallback)
            readback = self._creds.get("app-state", TRUST_HMAC_KEY)
            if readback == key:
                return key
        logger.warning("Could not store HMAC key in keyring")
        return None

    def _compute_hmac(self, data_bytes: bytes, key_hex: str) -> str:
        """Compute HMAC-SHA256 of data using the hex-encoded key."""
        key = bytes.fromhex(key_hex)
        return hmac.new(key, data_bytes, hashlib.sha256).hexdigest()

    def _verify_hmac(
        self, data_bytes: bytes, expected_hmac: str, key_hex: str,
    ) -> bool:
        """Verify HMAC-SHA256 using constant-time comparison."""
        key = bytes.fromhex(key_hex)
        computed = hmac.new(key, data_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, expected_hmac)

    # ── File I/O ──────────────────────────────────────────────────

    def _read_trust_file(self) -> Optional[Dict[str, Any]]:
        """Read and verify trust data from file.

        Returns:
            Trust data dict, or None if file missing/invalid/tampered
        """
        from .json_compat import json

        if not self._trust_file.exists():
            return None

        try:
            with open(self._trust_file, "r", encoding="utf-8") as f:
                wrapper = json.load(f)
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.warning("Cannot read trust file: %s", e)
            return None

        if not isinstance(wrapper, dict):
            return None

        data_dict = wrapper.get("data")
        stored_hmac = wrapper.get("hmac")

        if not isinstance(data_dict, dict):
            logger.warning("Trust file has no valid 'data' field")
            return None

        # Verify HMAC if we have a keyring key
        hmac_key = self._creds.get("app-state", TRUST_HMAC_KEY)
        if hmac_key and len(hmac_key) == 64:
            if not stored_hmac:
                logger.warning(
                    "Trust file has no HMAC — unsigned data, re-signing"
                )
            elif not self._verify_hmac(
                json.dumps(data_dict, separators=(",", ":"), sort_keys=True)
                .encode("utf-8"),
                stored_hmac,
                hmac_key,
            ):
                logger.warning(
                    "Trust file HMAC mismatch — possible tampering detected, "
                    "reinitializing trust data"
                )
                return None
        else:
            logger.debug("No HMAC key in keyring — skipping HMAC verification")

        return data_dict

    def _write_trust_file(self, data: Dict[str, Any]) -> bool:
        """Write trust data to file with HMAC signature.

        Returns:
            True if written successfully
        """
        from .json_compat import json
        import oschmod

        data_json = json.dumps(data, separators=(",", ":"), sort_keys=True)
        data_bytes = data_json.encode("utf-8")

        # Compute HMAC if keyring key available
        hmac_key = self._get_or_create_hmac_key()
        signature = (
            self._compute_hmac(data_bytes, hmac_key) if hmac_key else None
        )

        wrapper = {"data": data, "hmac": signature}

        try:
            self._trust_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._trust_file, "w", encoding="utf-8") as f:
                json.dump(wrapper, f, separators=(",", ":"), sort_keys=True)

            # Restrictive permissions (user-only)
            try:
                oschmod.set_mode(str(self._trust_file), 0o600)
            except (PermissionError, OSError):
                pass

            if hmac_key:
                logger.debug("Trust data saved with HMAC signature")
            else:
                logger.warning(
                    "Trust data saved WITHOUT HMAC — keyring unavailable"
                )
            return True
        except (IOError, OSError) as e:
            logger.error("Failed to write trust file: %s", e)
            return False

    # ── Migration from v1 (keyring-only) ──────────────────────────

    def _migrate_from_keyring(self) -> Optional[Dict[str, Any]]:
        """Check for legacy v1 trust data in keyring and migrate.

        Returns:
            Migrated trust data dict, or None if no legacy data
        """
        from .json_compat import json

        raw = self._creds.get("app-state", TRUST_KEY)
        if not raw:
            return None

        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            if data.get("v") != TRUST_SCHEMA_VERSION:
                return None
        except (json.JSONDecodeError, TypeError):
            return None

        logger.info("Migrating trust data from keyring to HMAC-signed file")

        # Write to new format
        if self._write_trust_file(data):
            # Clean up legacy keyring entry (it's too big for Windows anyway)
            try:
                self._creds.delete("app-state", TRUST_KEY)
                logger.info("Removed legacy trust data from keyring")
            except Exception:
                pass  # Non-critical — old entry just wastes space

        return data

    # ── Public API ────────────────────────────────────────────────

    def load(self) -> Dict[str, Any]:
        """Load trust data from HMAC-signed file (or migrate from keyring).

        Returns:
            Trust data dict (empty structure if not found)
        """
        if self._trust_data is not None:
            return self._trust_data

        # Try file-based storage (v2)
        data = self._read_trust_file()
        if data and data.get("v") == TRUST_SCHEMA_VERSION:
            self._trust_data = data
            return self._trust_data

        # Try migration from keyring (v1)
        data = self._migrate_from_keyring()
        if data:
            self._trust_data = data
            return self._trust_data

        # No valid data found — create empty structure
        self._trust_data = self._empty_trust_data()
        return self._trust_data

    def save(self) -> bool:
        """Persist trust data to HMAC-signed file.

        Returns:
            True if saved successfully
        """
        if self._trust_data is None:
            return True
        return self._write_trust_file(self._trust_data)

    def get_fingerprint(self, plugin_name: str) -> Optional[Dict[str, str]]:
        """Get stored fingerprint for a plugin.

        Returns:
            Dict with "version" and "hash" keys, or None
        """
        data = self.load()
        fp = data.get("fingerprints", {}).get(plugin_name)
        if fp:
            return fp
        return data.get("user_trusted", {}).get(plugin_name)

    def set_fingerprint(
        self, plugin_name: str, version: str, fingerprint: str,
        *, is_bundled: bool = True
    ) -> None:
        """Store a plugin fingerprint.

        Args:
            plugin_name: Plugin identifier
            version: Plugin version string
            fingerprint: SHA-256 Merkle hash
            is_bundled: Whether this is a bundled plugin
        """
        data = self.load()
        now = datetime.now(timezone.utc).isoformat()

        entry = {"version": version, "hash": fingerprint}
        if is_bundled:
            data.setdefault("fingerprints", {})[plugin_name] = entry
        else:
            entry["trusted_at"] = now
            data.setdefault("user_trusted", {})[plugin_name] = entry

    def is_disabled(self, plugin_name: str) -> Optional[Dict[str, str]]:
        """Check if a plugin is disabled by the integrity system.

        Returns:
            Disable info dict with "reason" and "since", or None
        """
        data = self.load()
        return data.get("disabled_plugins", {}).get(plugin_name)

    def disable_plugin(self, plugin_name: str, reason: str) -> None:
        """Mark a plugin as disabled by the integrity system.

        Args:
            plugin_name: Plugin identifier
            reason: Why it was disabled (e.g. "fingerprint_mismatch")
        """
        data = self.load()
        now = datetime.now(timezone.utc).isoformat()
        data.setdefault("disabled_plugins", {})[plugin_name] = {
            "reason": reason,
            "since": now,
        }

    def enable_plugin(self, plugin_name: str) -> None:
        """Remove a plugin from the disabled list."""
        data = self.load()
        data.get("disabled_plugins", {}).pop(plugin_name, None)

    def get_trust_source(self) -> str:
        """Get the current trust source."""
        data = self.load()
        return data.get("trust_source", "unknown")

    def _empty_trust_data(self) -> Dict[str, Any]:
        """Create an empty trust data structure."""
        return {
            "v": TRUST_SCHEMA_VERSION,
            "fingerprints": {},
            "user_trusted": {},
            "disabled_plugins": {},
            "trust_source": "initial_install",
            "initialized_at": datetime.now(timezone.utc).isoformat(),
        }


# --- I.3c: Plugin Verification ---

class PluginVerifier:
    """Orchestrates plugin fingerprint verification.

    Computes Merkle hashes of plugin directories and compares them
    against the trust store. Manages trust state for each plugin.
    """

    def __init__(self, trust_store: TrustStore):
        self._trust_store = trust_store
        # plugin_name -> VerificationResult
        self._results: Dict[str, "VerificationResult"] = {}

    @property
    def results(self) -> Dict[str, "VerificationResult"]:
        """All verification results from the last run."""
        return self._results

    def verify_all(
        self,
        discovered: Dict[str, Any],
        *,
        source_tree_dir: Optional[Path] = None,
    ) -> Dict[str, "VerificationResult"]:
        """Verify all discovered plugins.

        Args:
            discovered: Dict of plugin_name -> PluginMetadata
            source_tree_dir: Path to source tree plugins dir (for dev mode)

        Returns:
            Dict of plugin_name -> VerificationResult
        """
        self._results.clear()
        first_run = not self._trust_store.load().get("fingerprints")

        for name, metadata in discovered.items():
            result = self._verify_single(name, metadata, source_tree_dir, first_run)
            self._results[name] = result

        # If this is a first run, save all the seeded fingerprints
        if first_run:
            self._trust_store.save()
            logger.info("Trust store initialized from current installation")

        return self._results

    def _verify_single(
        self,
        plugin_name: str,
        metadata,
        source_tree_dir: Optional[Path],
        first_run: bool,
    ) -> "VerificationResult":
        """Verify a single plugin.

        Args:
            plugin_name: Plugin identifier
            metadata: PluginMetadata object
            source_tree_dir: Source tree plugins dir (dev mode detection)
            first_run: Whether this is the first run (seed mode)

        Returns:
            VerificationResult
        """
        plugin_dir = metadata.plugin_dir
        if not plugin_dir:
            return VerificationResult(
                plugin_name=plugin_name,
                trust_state=TrustState.MISMATCH,
                trust_tier=TrustTier.UNTRUSTED,
                reason="no plugin directory",
            )

        # Dev mode: source tree plugins skip verification
        if source_tree_dir and _is_under_source_tree(plugin_dir, source_tree_dir):
            return VerificationResult(
                plugin_name=plugin_name,
                trust_state=TrustState.SOURCE_TREE,
                trust_tier=TrustTier.BUNDLED if metadata.is_bundled else TrustTier.TRUSTED,
                reason="source tree",
            )

        # Compute current fingerprint
        current_hash = compute_plugin_fingerprint(plugin_dir)
        if not current_hash:
            return VerificationResult(
                plugin_name=plugin_name,
                trust_state=TrustState.MISMATCH,
                trust_tier=TrustTier.UNTRUSTED,
                reason="fingerprint computation failed",
            )

        tier = TrustTier.BUNDLED if metadata.is_bundled else TrustTier.UNTRUSTED

        # Check if integrity system previously disabled this plugin
        disable_info = self._trust_store.is_disabled(plugin_name)
        if disable_info:
            # Re-verify — maybe user reinstalled
            stored = self._trust_store.get_fingerprint(plugin_name)
            if stored and stored.get("hash") == current_hash:
                # Reinstall fixed it — re-enable
                self._trust_store.enable_plugin(plugin_name)
                logger.info(
                    "Plugin '%s' fingerprint now matches after reinstall — re-enabled",
                    plugin_name,
                )
                return VerificationResult(
                    plugin_name=plugin_name,
                    trust_state=TrustState.VERIFIED,
                    trust_tier=tier,
                    fingerprint=current_hash,
                    version=metadata.version,
                    reason="re-verified after reinstall",
                )
            else:
                if metadata.is_bundled:
                    # Bundled plugins always re-trusted — they ship with the app
                    self._trust_store.enable_plugin(plugin_name)
                    self._trust_store.set_fingerprint(
                        plugin_name, metadata.version, current_hash,
                        is_bundled=True,
                    )
                    logger.info(
                        "Bundled plugin '%s' re-enabled and fingerprint refreshed "
                        "(bundled plugins are always trusted)",
                        plugin_name,
                    )
                    return VerificationResult(
                        plugin_name=plugin_name,
                        trust_state=TrustState.VERIFIED,
                        trust_tier=tier,
                        fingerprint=current_hash,
                        version=metadata.version,
                        reason="bundled re-trust (re-enabled)",
                    )

                # Third-party: still mismatched — stay disabled
                return VerificationResult(
                    plugin_name=plugin_name,
                    trust_state=TrustState.MISMATCH,
                    trust_tier=tier,
                    fingerprint=current_hash,
                    version=metadata.version,
                    reason=f"disabled: {disable_info.get('reason', 'unknown')}",
                    disabled_since=disable_info.get("since"),
                )

        # First run: seed the trust store
        if first_run and metadata.is_bundled:
            self._trust_store.set_fingerprint(
                plugin_name, metadata.version, current_hash, is_bundled=True
            )
            return VerificationResult(
                plugin_name=plugin_name,
                trust_state=TrustState.VERIFIED,
                trust_tier=tier,
                fingerprint=current_hash,
                version=metadata.version,
                reason="initial seed",
            )

        # Look up stored fingerprint
        stored = self._trust_store.get_fingerprint(plugin_name)

        if not stored:
            if metadata.is_bundled:
                # New bundled plugin (added in update) — trust it
                self._trust_store.set_fingerprint(
                    plugin_name, metadata.version, current_hash, is_bundled=True
                )
                return VerificationResult(
                    plugin_name=plugin_name,
                    trust_state=TrustState.VERIFIED,
                    trust_tier=tier,
                    fingerprint=current_hash,
                    version=metadata.version,
                    reason="new bundled plugin — trusted",
                )
            else:
                # Unknown third-party plugin — untrusted
                return VerificationResult(
                    plugin_name=plugin_name,
                    trust_state=TrustState.UNVERIFIED,
                    trust_tier=TrustTier.UNTRUSTED,
                    fingerprint=current_hash,
                    version=metadata.version,
                    reason="new third-party plugin",
                )

        # Compare fingerprints
        stored_hash = stored.get("hash", "")
        if current_hash == stored_hash:
            return VerificationResult(
                plugin_name=plugin_name,
                trust_state=TrustState.VERIFIED,
                trust_tier=tier,
                fingerprint=current_hash,
                version=metadata.version,
            )

        # Bundled plugins always re-trusted — they ship with the app
        if metadata.is_bundled:
            self._trust_store.set_fingerprint(
                plugin_name, metadata.version, current_hash, is_bundled=True
            )
            logger.info(
                "Bundled plugin '%s' fingerprint refreshed "
                "(bundled plugins are always trusted)",
                plugin_name,
            )
            return VerificationResult(
                plugin_name=plugin_name,
                trust_state=TrustState.VERIFIED,
                trust_tier=tier,
                fingerprint=current_hash,
                version=metadata.version,
                reason="bundled re-trust",
            )

        # Third-party: fingerprint mismatch — possible tampering
        logger.warning(
            "Plugin '%s' fingerprint mismatch: expected %.12s, computed %.12s",
            plugin_name, stored_hash, current_hash,
        )
        return VerificationResult(
            plugin_name=plugin_name,
            trust_state=TrustState.MISMATCH,
            trust_tier=tier,
            fingerprint=current_hash,
            version=metadata.version,
            reason="fingerprint mismatch",
            expected_hash=stored_hash,
        )

    def get_failed_plugins(self) -> List[str]:
        """Get names of plugins that failed verification."""
        return [
            name for name, r in self._results.items()
            if r.trust_state == TrustState.MISMATCH
        ]

    def get_unverified_plugins(self) -> List[str]:
        """Get names of unverified third-party plugins."""
        return [
            name for name, r in self._results.items()
            if r.trust_state == TrustState.UNVERIFIED
        ]


class VerificationResult:
    """Result of verifying a single plugin."""

    __slots__ = (
        "plugin_name", "trust_state", "trust_tier", "fingerprint",
        "version", "reason", "expected_hash", "disabled_since",
    )

    def __init__(
        self,
        plugin_name: str,
        trust_state: str,
        trust_tier: str,
        fingerprint: Optional[str] = None,
        version: Optional[str] = None,
        reason: Optional[str] = None,
        expected_hash: Optional[str] = None,
        disabled_since: Optional[str] = None,
    ):
        self.plugin_name = plugin_name
        self.trust_state = trust_state
        self.trust_tier = trust_tier
        self.fingerprint = fingerprint
        self.version = version
        self.reason = reason
        self.expected_hash = expected_hash
        self.disabled_since = disabled_since

    @property
    def is_trusted(self) -> bool:
        """Whether the plugin passed verification."""
        return self.trust_state in (TrustState.VERIFIED, TrustState.SOURCE_TREE)


def _is_under_source_tree(plugin_dir: Path, source_tree_dir: Path) -> bool:
    """Check if a plugin directory is under the source tree."""
    try:
        plugin_dir.resolve().relative_to(source_tree_dir.resolve())
        return True
    except ValueError:
        return False


# --- I.3f: Distribution Format Detection ---

def detect_distribution_format() -> Tuple[str, Optional[str]]:
    """Detect how luducat was installed/distributed.

    Returns:
        Tuple of (format_name, detail_path_or_info)
    """
    # PyInstaller frozen build
    if getattr(sys, "frozen", False):
        bundle_dir = getattr(sys, "_MEIPASS", None)
        return ("pyinstaller", str(bundle_dir) if bundle_dir else None)

    # Nuitka compiled build
    if "__compiled__" in dir():
        return ("nuitka", sys.executable)

    # AppImage
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return ("appimage", appimage)

    # Flatpak
    if os.environ.get("FLATPAK_ID"):
        return ("flatpak", os.environ.get("FLATPAK_ID"))

    # Snap
    if os.environ.get("SNAP"):
        return ("snap", os.environ.get("SNAP"))

    # pip install (site-packages)
    main_module = Path(__file__).resolve()
    for sp in (sys.prefix, sys.base_prefix):
        try:
            main_module.relative_to(Path(sp) / "lib")
            return ("pip_install", str(main_module.parent.parent))
        except ValueError:
            pass

    # pip install -e (editable) or source tree
    return ("source", str(Path(__file__).resolve().parent.parent))


# --- I.3e: Trust State Logging ---

def log_trust_state(
    results: Dict[str, VerificationResult],
    trust_source: str,
    distribution: Tuple[str, Optional[str]],
    *,
    event: str = "startup",
) -> None:
    """Log the plugin trust state block.

    Printed at startup and shutdown for forensic visibility.

    Args:
        results: Verification results for all plugins
        trust_source: Trust data source (e.g. "initial_install", "proxy")
        distribution: Distribution format tuple from detect_distribution_format()
        event: "startup" or "shutdown"
    """
    dist_name, dist_detail = distribution

    logger.info("=== Plugin Trust State (%s) ===", event)
    logger.info(
        "Trust source: %s | App: %s | Distribution: %s%s",
        trust_source,
        APP_VERSION,
        dist_name,
        f" ({dist_detail})" if dist_detail else "",
    )

    if not results:
        logger.info("  (no plugins discovered)")
    else:
        for name in sorted(results):
            r = results[name]
            version = r.version or "?"
            state = r.trust_state.upper()
            tier = f"({r.trust_tier})"

            extra = ""
            if r.reason and r.trust_state != TrustState.VERIFIED:
                extra = f" — {r.reason}"
            if r.disabled_since:
                extra += f" since {r.disabled_since[:10]}"

            logger.info(
                "  %-28s %-8s %-12s %s%s",
                name, version, state, tier, extra,
            )

    # Summary counts
    verified = sum(1 for r in results.values() if r.is_trusted)
    failed = sum(
        1 for r in results.values()
        if r.trust_state == TrustState.MISMATCH
    )
    unverified = sum(
        1 for r in results.values()
        if r.trust_state == TrustState.UNVERIFIED
    )

    parts = [f"{verified} verified"]
    if failed:
        parts.append(f"{failed} FAILED")
    if unverified:
        parts.append(f"{unverified} unverified")
    logger.info("  Summary: %s", ", ".join(parts))
    logger.info("=" * 40)
