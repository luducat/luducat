# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# wine_env.py

"""Wine Environment Variable Accumulator

Composition primitive for building Wine launch environments. Each layer
(prefix, runner, sub-plugin, user overrides) contributes variables through
a first-writer-wins policy unless explicitly overridden.
"""

from typing import Dict, List, Optional


class WineEnv:
    """Accumulates environment variables for a Wine launch.

    First writer wins unless ``override=True``. Special handling for
    WINEDLLOVERRIDES (semicolon-joined) and command prefix wrappers.
    """

    def __init__(self, inherit_system: bool = True):
        self._inherit_system = inherit_system
        self._env: Dict[str, str] = {}
        self._dll_overrides: List[str] = []
        self._command_prefix: List[str] = []
        self._removed: set = set()
        self._empty_keys: set = set()

    def add(self, key: str, value: str, override: bool = False) -> None:
        """Add an environment variable. First writer wins unless override."""
        if key in self._removed:
            if not override:
                return
            self._removed.discard(key)
        if key in self._empty_keys:
            if not override:
                return
            self._empty_keys.discard(key)
        if key in self._env and not override:
            return
        self._env[key] = value

    def add_bundle(self, bundle: Dict[str, str], override: bool = False) -> None:
        """Add multiple variables from a dict."""
        for key, value in bundle.items():
            self.add(key, value, override=override)

    def concat(self, key: str, value: str, sep: str = ":") -> None:
        """Append to a path-like variable (e.g. LD_LIBRARY_PATH)."""
        if key in self._removed:
            return
        existing = self._env.get(key)
        if existing:
            self._env[key] = existing + sep + value
        else:
            self._env[key] = value

    def remove(self, key: str) -> None:
        """Remove a variable (will not appear in final env)."""
        self._env.pop(key, None)
        self._empty_keys.discard(key)
        self._removed.add(key)

    def set_empty(self, key: str) -> None:
        """Set a variable to empty string (significant for Wine)."""
        self._env.pop(key, None)
        self._removed.discard(key)
        self._empty_keys.add(key)

    def has(self, key: str) -> bool:
        """Check if a variable is set (including empty)."""
        return key in self._env or key in self._empty_keys

    def add_dll_override(self, dll: str, mode: str = "native,builtin") -> None:
        """Add a DLL override entry for WINEDLLOVERRIDES."""
        self._dll_overrides.append(f"{dll}={mode}")

    def add_command_prefix(self, *args: str) -> None:
        """Add wrapper commands (gamescope, mangohud, etc.)."""
        self._command_prefix.extend(args)

    def get_env(self) -> Dict[str, str]:
        """Build final environment dict.

        Merges system env (if inherit_system), accumulated vars, empty keys,
        and joins WINEDLLOVERRIDES from dll_override entries.
        """
        import os

        result: Dict[str, str] = {}
        if self._inherit_system:
            result.update(os.environ)

        # Remove keys first
        for key in self._removed:
            result.pop(key, None)

        # Apply accumulated vars
        result.update(self._env)

        # Apply empty keys
        for key in self._empty_keys:
            result[key] = ""

        # Join DLL overrides
        if self._dll_overrides:
            existing = result.get("WINEDLLOVERRIDES", "")
            joined = ";".join(self._dll_overrides)
            if existing:
                result["WINEDLLOVERRIDES"] = existing + ";" + joined
            else:
                result["WINEDLLOVERRIDES"] = joined

        return result

    def get_command_prefix(self) -> List[str]:
        """Get command prefix list (wrappers like gamescope, mangohud)."""
        return list(self._command_prefix)
