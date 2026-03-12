# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# storage.py

"""Path-confined filesystem access for plugins.

Self-contained: zero imports from ``luducat.core``.

Every filesystem operation validates that the resolved path stays
within the plugin's designated directory.  Path traversal attempts
(``../../../etc/passwd``) raise ``PluginStorageError``.

Usage in plugins::

    # Injected by PluginManager — access via self.storage
    self.storage.write_text("state.json", '{"cursor": 42}')
    data = self.storage.read_text("state.json")
    self.storage.ensure_dir("cache/thumbnails")
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PluginStorageError(Exception):
    """Raised on path traversal or other storage violations."""
    pass


class PluginStorage:
    """Path-confined filesystem access for plugins.

    Each plugin receives its own ``PluginStorage`` with three root
    directories: ``config``, ``cache``, ``data``.  All operations are
    confined to these directories.

    Args:
        plugin_name: Plugin identifier (for logging)
        config_dir: Absolute path to plugin's config directory
        cache_dir: Absolute path to plugin's cache directory
        data_dir: Absolute path to plugin's data directory
    """

    _BASE_DIRS = ("config", "cache", "data")

    def __init__(
        self,
        plugin_name: str,
        config_dir: Path,
        cache_dir: Path,
        data_dir: Path,
    ):
        self._plugin_name = plugin_name
        self._roots: Dict[str, Path] = {
            "config": config_dir.resolve(),
            "cache": cache_dir.resolve(),
            "data": data_dir.resolve(),
        }

    # ── Path resolution (internal) ───────────────────────────────────

    def _resolve(self, relative_path: str, base: str) -> Path:
        """Resolve a relative path within the given base directory.

        Raises ``PluginStorageError`` if the resolved path escapes
        the plugin's designated directory.
        """
        if base not in self._roots:
            raise PluginStorageError(
                f"Invalid base directory '{base}' — "
                f"must be one of {', '.join(self._BASE_DIRS)}"
            )
        root = self._roots[base]
        target = (root / relative_path).resolve()

        # Containment check: resolved path must be within root
        try:
            target.relative_to(root)
        except ValueError:
            raise PluginStorageError(
                f"Path traversal blocked for plugin '{self._plugin_name}': "
                f"'{relative_path}' resolves to '{target}' which is outside "
                f"'{root}'"
            ) from None
        return target

    # ── Public API ───────────────────────────────────────────────────

    def read_file(self, relative_path: str, base: str = "data") -> bytes:
        """Read a file as bytes.

        Args:
            relative_path: Path relative to the base directory
            base: Root directory — ``"config"``, ``"cache"``, or ``"data"``

        Returns:
            File contents as bytes

        Raises:
            PluginStorageError: Path escapes plugin directory
            FileNotFoundError: File does not exist
        """
        path = self._resolve(relative_path, base)
        return path.read_bytes()

    def write_file(self, relative_path: str, content: bytes,
                   base: str = "data") -> None:
        """Write bytes to a file, creating parent directories.

        Args:
            relative_path: Path relative to the base directory
            content: Bytes to write
            base: Root directory — ``"config"``, ``"cache"``, or ``"data"``

        Raises:
            PluginStorageError: Path escapes plugin directory
        """
        path = self._resolve(relative_path, base)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def read_text(self, relative_path: str, base: str = "data",
                  encoding: str = "utf-8") -> str:
        """Read a file as text.

        Args:
            relative_path: Path relative to the base directory
            base: Root directory — ``"config"``, ``"cache"``, or ``"data"``
            encoding: Text encoding (default UTF-8)

        Returns:
            File contents as string

        Raises:
            PluginStorageError: Path escapes plugin directory
            FileNotFoundError: File does not exist
        """
        path = self._resolve(relative_path, base)
        return path.read_text(encoding=encoding)

    def write_text(self, relative_path: str, content: str,
                   base: str = "data", encoding: str = "utf-8") -> None:
        """Write text to a file, creating parent directories.

        Args:
            relative_path: Path relative to the base directory
            content: Text to write
            base: Root directory — ``"config"``, ``"cache"``, or ``"data"``
            encoding: Text encoding (default UTF-8)

        Raises:
            PluginStorageError: Path escapes plugin directory
        """
        path = self._resolve(relative_path, base)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding=encoding)

    def list_dir(self, relative_path: str = "", base: str = "data") -> List[str]:
        """List directory contents.

        Args:
            relative_path: Path relative to the base directory (empty = root)
            base: Root directory — ``"config"``, ``"cache"``, or ``"data"``

        Returns:
            List of entry names (files and directories)

        Raises:
            PluginStorageError: Path escapes plugin directory
        """
        path = self._resolve(relative_path, base)
        if not path.is_dir():
            return []
        return [entry.name for entry in path.iterdir()]

    def ensure_dir(self, relative_path: str, base: str = "data") -> Path:
        """Create a directory (and parents), returning the absolute path.

        Args:
            relative_path: Path relative to the base directory
            base: Root directory — ``"config"``, ``"cache"``, or ``"data"``

        Returns:
            Absolute ``Path`` to the created directory

        Raises:
            PluginStorageError: Path escapes plugin directory
        """
        path = self._resolve(relative_path, base)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def exists(self, relative_path: str, base: str = "data") -> bool:
        """Check if a path exists.

        Args:
            relative_path: Path relative to the base directory
            base: Root directory — ``"config"``, ``"cache"``, or ``"data"``

        Returns:
            ``True`` if the path exists

        Raises:
            PluginStorageError: Path escapes plugin directory
        """
        path = self._resolve(relative_path, base)
        return path.exists()

    def delete(self, relative_path: str, base: str = "data") -> bool:
        """Delete a file.

        Does NOT delete directories — use with caution.

        Args:
            relative_path: Path relative to the base directory
            base: Root directory — ``"config"``, ``"cache"``, or ``"data"``

        Returns:
            ``True`` if the file was deleted, ``False`` if it did not exist

        Raises:
            PluginStorageError: Path escapes plugin directory
            IsADirectoryError: Target is a directory
        """
        path = self._resolve(relative_path, base)
        if not path.exists():
            return False
        if path.is_dir():
            raise IsADirectoryError(
                f"Cannot delete directory '{relative_path}' — "
                f"use shutil via PluginStorage is not supported for directory removal"
            )
        path.unlink()
        return True

    def get_path(self, relative_path: str, base: str = "data") -> Path:
        """Resolve a relative path to an absolute path (validated).

        Useful when a plugin needs to pass a path to a library
        (e.g., SQLAlchemy database URL).

        Args:
            relative_path: Path relative to the base directory
            base: Root directory — ``"config"``, ``"cache"``, or ``"data"``

        Returns:
            Validated absolute ``Path``

        Raises:
            PluginStorageError: Path escapes plugin directory
        """
        return self._resolve(relative_path, base)

    def get_db_path(self, db_name: str = "plugin.db") -> Path:
        """Return validated path for the plugin's database file.

        Convenience method equivalent to ``get_path(db_name, "data")``.

        Args:
            db_name: Database filename (default ``"plugin.db"``)

        Returns:
            Absolute path within the plugin's data directory
        """
        return self._resolve(db_name, "data")

    def get_storage_usage(self) -> Dict[str, int]:
        """Return disk usage per base directory in bytes.

        Returns:
            Dict mapping base name to total bytes used.
            Directories that don't exist report 0.
        """
        usage: Dict[str, int] = {}
        for base_name, root in self._roots.items():
            if not root.exists():
                usage[base_name] = 0
                continue
            total = 0
            for dirpath, _dirnames, filenames in os.walk(root):
                for fname in filenames:
                    try:
                        total += (Path(dirpath) / fname).stat().st_size
                    except OSError:
                        pass
            usage[base_name] = total
        return usage
