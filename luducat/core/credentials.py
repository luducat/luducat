# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# credentials.py

"""Credential storage for luducat

Storage priority:
1. System keyring if available (libsecret, Keychain, Credential Manager)
2. Fallback to JSON file in config directory (less secure but functional)

Plugins should use this manager for credential storage.
"""

from luducat.core.json_compat import json
import logging
import oschmod
from pathlib import Path
from typing import Optional

from .constants import APP_NAME

logger = logging.getLogger(__name__)

# Try to import keyring, but provide fallback
try:
    import keyring
    from keyring.errors import PasswordDeleteError
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False
    keyring = None  # type: ignore[assignment]
    PasswordDeleteError = Exception  # type: ignore[assignment,misc]
    logger.debug("keyring package not available - using file-based fallback")


class CredentialManager:
    """Credential storage manager with fallback support

    Provides a simple interface for storing and retrieving credentials.
    Uses system keyring when available, falls back to file-based storage.

    Usage:
        creds = CredentialManager(config_dir)

        # Store credential
        creds.store("steam", "api_key", "abc123")

        # Retrieve credential
        api_key = creds.get("steam", "api_key")

        # Delete credential
        creds.delete("steam", "api_key")
    """

    def __init__(self, config_dir: Optional[Path] = None, service_prefix: str = APP_NAME):
        """Initialize credential manager

        Args:
            config_dir: Directory for fallback credential file storage
            service_prefix: Prefix for keyring service names
        """
        self.service_prefix = service_prefix
        self._use_keyring = KEYRING_AVAILABLE

        # Set up fallback file storage
        if config_dir is None:
            import platformdirs
            config_dir = Path(platformdirs.user_config_dir(APP_NAME))
        self._credentials_file = config_dir / "credentials.json"
        self._credentials_cache: Optional[dict] = None

        if self._use_keyring:
            logger.debug("Using system keyring for credential storage")
        else:
            logger.info(f"Using file-based credential storage: {self._credentials_file}")

    @property
    def is_available(self) -> bool:
        """Check if credential storage is available (always True with fallback)"""
        return True

    def _get_service_name(self, plugin_name: str) -> str:
        """Get full service name for a plugin

        Args:
            plugin_name: Plugin identifier

        Returns:
            Full service name (e.g., "luducat.steam")
        """
        return f"{self.service_prefix}.{plugin_name}"

    # ---- File-based storage methods ----

    def _load_credentials_file(self) -> dict:
        """Load credentials from JSON file"""
        if self._credentials_cache is not None:
            return self._credentials_cache

        if not self._credentials_file.exists():
            self._credentials_cache = {}
            return self._credentials_cache

        try:
            with open(self._credentials_file, 'r') as f:
                self._credentials_cache = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load credentials file: {e}")
            self._credentials_cache = {}

        return self._credentials_cache

    def _save_credentials_file(self) -> bool:
        """Save credentials to JSON file"""
        if self._credentials_cache is None:
            return True

        try:
            # Ensure directory exists
            self._credentials_file.parent.mkdir(parents=True, exist_ok=True)

            # Write with restrictive permissions
            with open(self._credentials_file, 'w') as f:
                json.dump(self._credentials_cache, f, indent=2)



            # Set file permissions to user-only (600)

            # do it safely across platforms
            try:
                oschmod.set_mode(str(self._credentials_file), 0o600)
            except (PermissionError, FileNotFoundError) as e:
                logger.error(f"Failed to save credentials file: {e}, permission denied.")
                print(f"Error: {e}")
            except OSError as e:
                logger.error(f"Failed to save credentials file: {e}, general error.")
                print(f"OS error: {e}")

            return True
        except IOError as e:
            logger.error(f"Failed to save credentials file: {e}")
            return False

    def _file_store(self, plugin_name: str, key: str, value: str) -> bool:
        """Store credential in file"""
        creds = self._load_credentials_file()
        if plugin_name not in creds:
            creds[plugin_name] = {}
        creds[plugin_name][key] = value
        return self._save_credentials_file()

    def _file_get(self, plugin_name: str, key: str) -> Optional[str]:
        """Get credential from file"""
        creds = self._load_credentials_file()
        return creds.get(plugin_name, {}).get(key)

    def _file_delete(self, plugin_name: str, key: str) -> bool:
        """Delete credential from file"""
        creds = self._load_credentials_file()
        if plugin_name in creds and key in creds[plugin_name]:
            del creds[plugin_name][key]
            if not creds[plugin_name]:
                del creds[plugin_name]
            return self._save_credentials_file()
        return True

    # ---- Public API methods ----

    def store(self, plugin_name: str, key: str, value: str) -> bool:
        """Store a credential

        Args:
            plugin_name: Plugin identifier (e.g., "steam")
            key: Credential key (e.g., "api_key", "auth_token")
            value: Credential value

        Returns:
            True if stored successfully, False otherwise
        """
        if self._use_keyring:
            service = self._get_service_name(plugin_name)
            try:
                keyring.set_password(service, key, value)
                logger.debug(f"Stored credential in keyring: {service}/{key}")
                return True
            except Exception as e:
                logger.warning(f"Keyring failed, using file fallback: {e}")
                # Fall through to file storage

        return self._file_store(plugin_name, key, value)

    def get(self, plugin_name: str, key: str) -> Optional[str]:
        """Retrieve a credential

        Args:
            plugin_name: Plugin identifier
            key: Credential key

        Returns:
            Credential value or None if not found
        """
        if self._use_keyring:
            service = self._get_service_name(plugin_name)
            try:
                value = keyring.get_password(service, key)
                if value is not None:
                    return value
            except Exception as e:
                logger.warning(f"Keyring failed, checking file fallback: {e}")

        # Try file storage (as fallback or primary)
        return self._file_get(plugin_name, key)

    def delete(self, plugin_name: str, key: str) -> bool:
        """Delete a credential

        Args:
            plugin_name: Plugin identifier
            key: Credential key

        Returns:
            True if deleted successfully, False otherwise
        """
        success = True

        if self._use_keyring:
            service = self._get_service_name(plugin_name)
            try:
                keyring.delete_password(service, key)
                logger.debug(f"Deleted credential from keyring: {service}/{key}")
            except PasswordDeleteError:
                pass  # Credential didn't exist - that's fine
            except Exception as e:
                logger.warning(f"Failed to delete from keyring: {e}")
                success = False

        # Also delete from file storage
        if not self._file_delete(plugin_name, key):
            success = False

        return success

    def has_credential(self, plugin_name: str, key: str) -> bool:
        """Check if a credential exists

        Args:
            plugin_name: Plugin identifier
            key: Credential key

        Returns:
            True if credential exists
        """
        return self.get(plugin_name, key) is not None

    def clear_plugin_credentials(self, plugin_name: str, keys: list[str]) -> None:
        """Clear all known credentials for a plugin

        Args:
            plugin_name: Plugin identifier
            keys: List of credential keys to clear
        """
        for key in keys:
            self.delete(plugin_name, key)
        logger.info(f"Cleared credentials for plugin: {plugin_name}")


# Convenience function for testing keyring availability
def test_keyring() -> bool:
    """Test if keyring is working

    Attempts to store and retrieve a test credential.

    Returns:
        True if keyring is functional
    """
    if not KEYRING_AVAILABLE:
        return False

    test_service = f"{APP_NAME}.test"
    test_key = "test_credential"
    test_value = "test_value_12345"

    try:
        # Store
        keyring.set_password(test_service, test_key, test_value)

        # Retrieve
        retrieved = keyring.get_password(test_service, test_key)

        # Clean up
        try:
            keyring.delete_password(test_service, test_key)
        except Exception:
            pass

        return retrieved == test_value

    except Exception as e:
        logger.warning(f"Keyring test failed: {e}")
        return False
