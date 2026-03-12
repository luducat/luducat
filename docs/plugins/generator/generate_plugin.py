#!/usr/bin/env python3
"""luducat Plugin Generator

Generates a complete plugin project directory with all boilerplate files.
Can be run interactively (no arguments) or via CLI flags.

This generator is GPLv3. Generated plugin code is under the license you choose.

Usage:
    python generate_plugin.py                           # Interactive
    python generate_plugin.py --name my_store --type store --license MIT
    python generate_plugin.py --help
"""

import argparse
import os
import sys
import textwrap
from pathlib import Path

# ── License Templates ────────────────────────────────────────────────

_LICENSES = {
    "MIT": textwrap.dedent("""\
        MIT License

        Copyright (c) {year} {author}

        Permission is hereby granted, free of charge, to any person obtaining a copy
        of this software and associated documentation files (the "Software"), to deal
        in the Software without restriction, including without limitation the rights
        to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
        copies of the Software, and to permit persons to whom the Software is
        furnished to do so, subject to the following conditions:

        The above copyright notice and this permission notice shall be included in all
        copies or substantial portions of the Software.

        THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
        IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
        FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
        AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
        LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
        OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
        SOFTWARE.
    """),
    "Apache-2.0": textwrap.dedent("""\
        Copyright {year} {author}

        Licensed under the Apache License, Version 2.0 (the "License");
        you may not use this file except in compliance with the License.
        You may obtain a copy of the License at

            http://www.apache.org/licenses/LICENSE-2.0

        Unless required by applicable law or agreed to in writing, software
        distributed under the License is distributed on an "AS IS" BASIS,
        WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
        See the License for the specific language governing permissions and
        limitations under the License.
    """),
    "GPLv2+": textwrap.dedent("""\
        Copyright (C) {year} {author}

        This program is free software; you can redistribute it and/or modify
        it under the terms of the GNU General Public License as published by
        the Free Software Foundation; either version 2 of the License, or
        (at your option) any later version.

        This program is distributed in the hope that it will be useful,
        but WITHOUT ANY WARRANTY; without even the implied warranty of
        MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
        GNU General Public License for more details.
    """),
    "GPLv3": textwrap.dedent("""\
        Copyright (C) {year} {author}

        This program is free software: you can redistribute it and/or modify
        it under the terms of the GNU General Public License as published by
        the Free Software Foundation, either version 3 of the License, or
        (at your option) any later version.

        This program is distributed in the hope that it will be useful,
        but WITHOUT ANY WARRANTY; without even the implied warranty of
        MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
        GNU General Public License for more details.
    """),
}

# ── Plugin Type Templates ────────────────────────────────────────────

_TYPE_CONFIG = {
    "store": {
        "class_suffix": "Store",
        "base_class": "AbstractGameStore",
        "base_alias": "StorePlugin",
        "file_name": "store.py",
        "json_key": "store_class",
        "json_value_prefix": "store.",
        "capabilities": {
            "fetch_library": True,
            "fetch_metadata": True,
            "launch_games": True,
            "track_playtime": False,
        },
    },
    "metadata": {
        "class_suffix": "Provider",
        "base_class": "AbstractMetadataProvider",
        "base_alias": "MetadataPlugin",
        "file_name": "provider.py",
        "json_key": "provider_class",
        "json_value_prefix": "provider.",
        "capabilities": {
            "enrich_metadata": True,
            "search_games": True,
            "fetch_metadata": True,
        },
    },
    "platform": {
        "class_suffix": "Platform",
        "base_class": "AbstractPlatformProvider",
        "base_alias": "PlatformPlugin",
        "file_name": "platform.py",
        "json_key": "entry_point",
        "json_value_prefix": "platform.",
        "capabilities": {
            "platform_type": "custom",
            "game_types": [],
            "launch_method": "executable",
        },
    },
    "runner": {
        "class_suffix": "Runner",
        "base_class": "AbstractRunnerPlugin",
        "base_alias": "RunnerPlugin",
        "file_name": "runner.py",
        "json_key": "entry_point",
        "json_value_prefix": "runner.",
        "capabilities": {
            "supported_stores": [],
            "launch_method": "url_scheme",
        },
    },
}


def _to_class_name(plugin_name: str) -> str:
    """Convert plugin_name to PascalCase class name."""
    return "".join(word.capitalize() for word in plugin_name.split("_"))


def _generate_plugin_json(cfg: dict) -> str:
    """Generate plugin.json content."""
    import json

    type_cfg = _TYPE_CONFIG[cfg["type"]]
    class_name = _to_class_name(cfg["name"]) + type_cfg["class_suffix"]

    data = {
        "name": cfg["name"],
        "display_name": cfg["display_name"],
        "version": "0.1.0",
        "author": cfg["author"],
    }
    if cfg.get("email"):
        data["author_email"] = cfg["email"]
    data.update({
        "description": f"{cfg['display_name']} plugin for luducat",
        "min_luducat_version": "0.2.9.24",
        "plugin_types": [cfg["type"]],
        type_cfg["json_key"]: type_cfg["json_value_prefix"] + class_name,
        "capabilities": type_cfg["capabilities"],
        "auth": {"type": "none"},
        "network": {"allowed_domains": []},
        "privacy": {
            "telemetry": False,
            "data_collection": "none",
            "third_party_services": [],
        },
        "settings_schema": {},
    })

    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _generate_store_py(cfg: dict) -> str:
    class_name = _to_class_name(cfg["name"]) + "Store"
    return textwrap.dedent(f'''\
        """{{display_name}} store plugin for luducat."""

        import logging
        from pathlib import Path
        from typing import Callable, List, Optional

        from luducat.plugins.base import AbstractGameStore, Game

        logger = logging.getLogger(__name__)


        class {class_name}(AbstractGameStore):
            """{cfg["display_name"]} store integration.

            Implements the store plugin contract. All HTTP requests should use
            self.http (a PluginHttpClient instance set by the plugin manager).
            User settings are available via self.get_setting(key, default).
            """

            @property
            def store_name(self) -> str:
                return "{cfg["name"]}"

            @property
            def display_name(self) -> str:
                return "{cfg["display_name"]}"

            def is_available(self) -> bool:
                """Return True if this store can be used (e.g. credentials exist)."""
                return True

            def is_authenticated(self) -> bool:
                """Return True if the user is currently authenticated."""
                return True

            async def authenticate(self) -> bool:
                """Perform authentication. Return True on success."""
                return True

            async def fetch_user_games(
                self,
                status_callback: Optional[Callable[[str], None]] = None,
                cancel_check: Optional[Callable[[], bool]] = None,
            ) -> List[str]:
                """Fetch the user's owned game IDs from the store.

                Use status_callback("message") to report progress to the UI.
                Check cancel_check() periodically and return early if True.

                Returns:
                    List of store-specific app ID strings.
                """
                return []

            async def fetch_game_metadata(self, app_ids, download_images=False):
                """Fetch Game objects for the given app IDs.

                Args:
                    app_ids: List of store-specific app ID strings.
                    download_images: Whether to download cover/screenshot images.

                Returns:
                    List of Game objects with metadata populated.
                """
                return []

            def get_database_path(self) -> Path:
                """Return path to plugin's local cache database."""
                return self.data_dir / "catalog.db"
    ''').replace("{display_name}", cfg["display_name"])


def _generate_metadata_py(cfg: dict) -> str:
    class_name = _to_class_name(cfg["name"]) + "Provider"
    return textwrap.dedent(f'''\
        """{{display_name}} metadata plugin for luducat."""

        import logging
        from pathlib import Path
        from typing import Any, Dict, List, Optional

        from luducat.plugins.base import (
            AbstractMetadataProvider,
            EnrichmentData,
            MetadataSearchResult,
        )

        logger = logging.getLogger(__name__)


        class {class_name}(AbstractMetadataProvider):
            """{cfg["display_name"]} metadata provider.

            Implements the metadata provider contract. Metadata providers can:
            - Enrich games with additional data (genres, ratings, screenshots)
            - Sync tags/favourites/hidden status from external sources
            - Search for games by title

            For tag sync plugins, implement get_tag_sync_data() and set
            capabilities.tag_sync = true in plugin.json.

            For enrichment plugins, implement lookup_by_store_id(),
            search_game(), and get_enrichment().

            User settings: self.get_setting(key, default)
            Privacy check: self.has_local_data_consent() — required before
            reading local files from other applications.
            """

            @property
            def provider_name(self) -> str:
                return "{cfg["name"]}"

            @property
            def display_name(self) -> str:
                return "{cfg["display_name"]}"

            def is_available(self) -> bool:
                """Return True if this provider can be used (e.g. data source exists)."""
                return True

            def is_authenticated(self) -> bool:
                """Return True if authenticated. For local-only providers, return True."""
                return True

            async def authenticate(self) -> bool:
                """Perform authentication. For local-only providers, return True."""
                return True

            async def lookup_by_store_id(
                self, store_name: str, store_id: str
            ) -> Optional[str]:
                """Look up this provider's internal ID for a store game.

                Args:
                    store_name: luducat store name (e.g. "steam", "gog", "epic")
                    store_id: Store-specific app ID string.

                Returns:
                    Provider-specific ID string, or None if not found.
                """
                return None

            async def search_game(
                self, title: str, year: Optional[int] = None
            ) -> List[MetadataSearchResult]:
                """Search for games by title (fallback when lookup_by_store_id fails).

                Returns:
                    List of MetadataSearchResult with id, title, year fields.
                """
                return []

            async def get_enrichment(
                self, provider_id: str
            ) -> Optional[EnrichmentData]:
                """Fetch enrichment data for a game by provider ID.

                Returns:
                    EnrichmentData with metadata fields, or None.
                """
                return None

            def get_database_path(self) -> Path:
                """Return path to plugin's local database (for cache/enrichment data)."""
                return self.data_dir / "enrichment.db"

            # Uncomment to implement tag sync (also set tag_sync: true in plugin.json):
            #
            # def get_tag_sync_data(self, **kwargs) -> Optional[Dict[str, Any]]:
            #     """Return tag/favourite/hidden sync data from an external source.
            #
            #     Check self.has_local_data_consent() before reading local files.
            #
            #     Returns:
            #         {{
            #             "source": "{cfg["name"]}",
            #             "mode": "add_only",
            #             "entries": [
            #                 {{
            #                     "store": "steam",
            #                     "app_id": "220",
            #                     "title": "Half-Life 2",
            #                     "tags": ["FPS", "Completed"],
            #                     "favorite": True,
            #                     "hidden": False,
            #                 }},
            #             ],
            #         }}
            #         or None on failure.
            #     """
            #     if not self.has_local_data_consent():
            #         return None
            #     return None
    ''').replace("{display_name}", cfg["display_name"])


def _generate_platform_py(cfg: dict) -> str:
    class_name = _to_class_name(cfg["name"]) + "Platform"
    return textwrap.dedent(f'''\
        """{{display_name}} platform plugin for luducat."""

        import logging
        from pathlib import Path
        from typing import Any, Dict, List

        from luducat.plugins.base import AbstractPlatformProvider, Game

        logger = logging.getLogger(__name__)


        class {class_name}(AbstractPlatformProvider):
            """{cfg["display_name"]} platform provider.

            Platform plugins detect emulators/compatibility layers on the system
            and provide launch configurations for games that need them.

            Use luducat.plugins.sdk.app_finder to detect installed binaries.
            """

            @property
            def provider_name(self) -> str:
                return "{cfg["name"]}"

            @property
            def display_name(self) -> str:
                return "{cfg["display_name"]}"

            @property
            def platform_type(self) -> str:
                """Unique platform type identifier."""
                return "{cfg["name"]}"

            def detect_platforms(self) -> List[Dict[str, Any]]:
                """Scan the system for installed platform binaries.

                Returns:
                    List of dicts with platform info (path, version, etc.).
                """
                return []

            def can_run_game(self, game: Game) -> bool:
                """Return True if this platform can run the given game."""
                return False

            def create_launch_config(self, game, platform_info, **kwargs):
                """Build a launch configuration dict for a game.

                Args:
                    game: The Game object to launch.
                    platform_info: Platform info dict from detect_platforms().

                Returns:
                    Dict with launch configuration (executable, arguments, env, etc.).
                """
                return {{}}
    ''').replace("{display_name}", cfg["display_name"])


def _generate_runner_py(cfg: dict) -> str:
    class_name = _to_class_name(cfg["name"]) + "Runner"
    return textwrap.dedent(f'''\
        """{{display_name}} runner plugin for luducat."""

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


        class {class_name}(AbstractRunnerPlugin):
            """{cfg["display_name"]} runner.

            Runner plugins detect a game launcher on the system and build
            launch intents that tell luducat how to start a game through it.

            Launch methods:
            - LaunchMethod.URL_SCHEME: open a URI (e.g. "steam://rungameid/220")
            - LaunchMethod.EXECUTABLE: run a binary with arguments

            Use luducat.plugins.sdk.app_finder to detect installed launchers.
            """

            def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
                super().__init__(config_dir, cache_dir, data_dir)
                self._launcher_info: Optional[RunnerLauncherInfo] = None
                self._detection_done = False

            @property
            def runner_name(self) -> str:
                return "{cfg["name"]}"

            @property
            def display_name(self) -> str:
                return "{cfg["display_name"]}"

            @property
            def supported_stores(self) -> List[str]:
                """Store plugin names this runner can launch games for.

                Example: ["steam", "gog", "epic"]
                """
                return []

            def detect_launcher(self) -> Optional[RunnerLauncherInfo]:
                """Detect if the launcher is installed on this system.

                Returns:
                    RunnerLauncherInfo with path, install_type, capabilities,
                    or None if the launcher is not found.
                """
                if self._detection_done:
                    return self._launcher_info
                self._detection_done = True

                # Detect launcher binary using the SDK app_finder:
                from luducat.plugins.sdk.app_finder import find_application

                results = find_application(["{cfg["name"]}"])
                if not results:
                    return None

                r = results[0]
                self._launcher_info = RunnerLauncherInfo(
                    runner_name=self.runner_name,
                    path=r.path,
                    install_type=r.install_type,
                    virtualized=r.virtualized,
                )
                return self._launcher_info

            def build_launch_intent(
                self, store_name: str, app_id: str
            ) -> Optional[LaunchIntent]:
                """Build a launch intent for a game.

                Args:
                    store_name: luducat store name (e.g. "steam", "gog")
                    app_id: Store-specific app ID string.

                Returns:
                    LaunchIntent describing how to launch, or None if
                    this runner cannot launch the given game.
                """
                if store_name not in self.supported_stores:
                    return None

                info = self.detect_launcher()
                if not info:
                    return None

                # Replace with your launcher's URI or command:
                return None
    ''').replace("{display_name}", cfg["display_name"])


_GENERATORS = {
    "store": _generate_store_py,
    "metadata": _generate_metadata_py,
    "platform": _generate_platform_py,
    "runner": _generate_runner_py,
}


def _generate_init_py(cfg: dict) -> str:
    type_cfg = _TYPE_CONFIG[cfg["type"]]
    class_name = _to_class_name(cfg["name"]) + type_cfg["class_suffix"]
    file_stem = type_cfg["file_name"].replace(".py", "")
    return f"from .{file_stem} import {class_name}\n\n__all__ = [\"{class_name}\"]\n"


def _generate_test_py(cfg: dict) -> str:
    type_cfg = _TYPE_CONFIG[cfg["type"]]
    class_name = _to_class_name(cfg["name"]) + type_cfg["class_suffix"]
    file_stem = type_cfg["file_name"].replace(".py", "")
    return textwrap.dedent(f'''\
        """Tests for {cfg["display_name"]} plugin."""

        import pytest
        from pathlib import Path
        from unittest.mock import MagicMock

        from {cfg["name"]}.{file_stem} import {class_name}


        @pytest.fixture
        def plugin(tmp_path):
            config_dir = tmp_path / "config"
            cache_dir = tmp_path / "cache"
            data_dir = tmp_path / "data"
            config_dir.mkdir()
            cache_dir.mkdir()
            data_dir.mkdir()
            instance = {class_name}(config_dir, cache_dir, data_dir)
            instance.set_http_client(MagicMock())
            return instance


        def test_name(plugin):
            assert plugin.{"store_name" if cfg["type"] == "store" else "provider_name" if cfg["type"] == "metadata" else "runner_name" if cfg["type"] == "runner" else "provider_name"} == "{cfg["name"]}"


        def test_display_name(plugin):
            assert plugin.display_name == "{cfg["display_name"]}"
    ''')


def _generate_gitignore() -> str:
    return textwrap.dedent("""\
        __pycache__/
        *.py[cod]
        *.egg-info/
        dist/
        build/
        .eggs/
        .pytest_cache/
        *.db
    """)


def _generate_readme(cfg: dict) -> str:
    return textwrap.dedent(f"""\
        # {cfg["display_name"]} -- luducat Plugin

        {cfg["display_name"]} plugin for [luducat](https://github.com/luducat/luducat).

        ## Installation

        Copy this directory to your luducat plugins folder:

        ```bash
        cp -r {cfg["name"]} ~/.local/share/luducat/plugins/
        ```

        Then enable "{cfg["display_name"]}" in Settings > Plugins.

        ## Development

        ```bash
        # Run tests
        pytest tests/ -v

        # Build distribution
        bash build.sh
        ```

        ## License

        {cfg["license"]}
    """)


def _generate_build_sh(cfg: dict) -> str:
    return textwrap.dedent(f"""\
        #!/bin/bash
        # Build a distributable ZIP for the {cfg["display_name"]} plugin.
        set -e
        PLUGIN_NAME="{cfg["name"]}"
        VERSION=$(python3 -c "import json; print(json.load(open('plugin.json'))['version'])")
        DIST_DIR="dist"
        mkdir -p "$DIST_DIR"
        zip -r "$DIST_DIR/${{PLUGIN_NAME}}-${{VERSION}}.zip" \\
            plugin.json *.py README.md LICENSE \\
            --exclude '__pycache__/*' '*.pyc' 'tests/*' 'dist/*'
        echo "Built: $DIST_DIR/${{PLUGIN_NAME}}-${{VERSION}}.zip"
    """)


def _prompt(question: str, default: str = "") -> str:
    """Prompt user for input with optional default."""
    if default:
        answer = input(f"{question} [{default}]: ").strip()
        return answer or default
    while True:
        answer = input(f"{question}: ").strip()
        if answer:
            return answer
        print("  (required)")


def _prompt_choice(question: str, choices: list, default: str = "") -> str:
    """Prompt user to choose from a list."""
    print(f"\n{question}")
    for i, choice in enumerate(choices, 1):
        marker = " (default)" if choice == default else ""
        print(f"  {i}. {choice}{marker}")
    while True:
        answer = input(f"Choice [1-{len(choices)}]: ").strip()
        if not answer and default:
            return default
        try:
            idx = int(answer) - 1
            if 0 <= idx < len(choices):
                return choices[idx]
        except ValueError:
            if answer in choices:
                return answer
        print(f"  Please enter 1-{len(choices)}")


def interactive_mode() -> dict:
    """Run interactive questionnaire."""
    print("\n=== luducat Plugin Generator ===\n")

    cfg = {}
    cfg["name"] = _prompt("Plugin name (lowercase, underscores ok)", "").lower().replace("-", "_").replace(" ", "_")
    cfg["display_name"] = _prompt("Display name", cfg["name"].replace("_", " ").title())
    cfg["author"] = _prompt("Author name")
    cfg["email"] = input("Author email (optional): ").strip()
    cfg["type"] = _prompt_choice(
        "Plugin type:", ["store", "metadata", "platform", "runner"], "store"
    )
    cfg["license"] = _prompt_choice(
        "License:", list(_LICENSES.keys()), "GPLv2+"
    )
    cfg["output_dir"] = _prompt("Output directory", f"./{cfg['name']}")

    return cfg


def cli_mode(args: argparse.Namespace) -> dict:
    """Build config from CLI arguments."""
    return {
        "name": args.name.lower().replace("-", "_").replace(" ", "_"),
        "display_name": args.display_name or args.name.replace("_", " ").title(),
        "author": args.author or "Unknown",
        "email": args.email or "",
        "type": args.type,
        "license": args.license,
        "output_dir": args.output or f"./{args.name}",
    }


def generate(cfg: dict) -> Path:
    """Generate the plugin project."""
    from datetime import datetime

    output = Path(cfg["output_dir"])
    output.mkdir(parents=True, exist_ok=True)

    type_cfg = _TYPE_CONFIG[cfg["type"]]
    year = datetime.now().year

    # plugin.json
    (output / "plugin.json").write_text(_generate_plugin_json(cfg))

    # Main implementation file
    generator = _GENERATORS[cfg["type"]]
    (output / type_cfg["file_name"]).write_text(generator(cfg))

    # __init__.py
    (output / "__init__.py").write_text(_generate_init_py(cfg))

    # Tests
    tests_dir = output / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / f"test_{type_cfg['file_name']}").write_text(_generate_test_py(cfg))

    # LICENSE
    license_text = _LICENSES.get(cfg["license"], _LICENSES["GPLv2+"])
    (output / "LICENSE").write_text(
        license_text.format(year=year, author=cfg["author"])
    )

    # README.md
    (output / "README.md").write_text(_generate_readme(cfg))

    # .gitignore
    (output / ".gitignore").write_text(_generate_gitignore())

    # build.sh
    build_path = output / "build.sh"
    build_path.write_text(_generate_build_sh(cfg))
    build_path.chmod(0o755)

    return output


def main():
    parser = argparse.ArgumentParser(
        description="Generate a luducat plugin project",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s                                    Interactive mode
              %(prog)s --name battle_net --type store     Minimal CLI
              %(prog)s --name hltb --type metadata --license MIT --author "You"
        """),
    )
    parser.add_argument("--name", help="Plugin name (lowercase)")
    parser.add_argument("--display-name", help="Display name (default: derived from name)")
    parser.add_argument("--type", choices=["store", "metadata", "platform", "runner"],
                        help="Plugin type")
    parser.add_argument("--author", help="Author name")
    parser.add_argument("--email", help="Author email")
    parser.add_argument("--license", choices=list(_LICENSES.keys()),
                        default="GPLv2+", help="License (default: GPLv2+)")
    parser.add_argument("--output", "-o", help="Output directory")

    args = parser.parse_args()

    # If no required args, run interactive mode
    if not args.name or not args.type:
        if args.name or args.type:
            parser.error("--name and --type are both required for CLI mode")
        cfg = interactive_mode()
    else:
        cfg = cli_mode(args)

    output = generate(cfg)
    print(f"\nPlugin generated at: {output}")
    print(f"  Type: {cfg['type']}")
    print(f"  License: {cfg['license']}")
    print(f"\nNext steps:")
    print(f"  1. Edit {output / _TYPE_CONFIG[cfg['type']]['file_name']}")
    print(f"  2. Update plugin.json with capabilities and network domains")
    print(f"  3. Run: pytest {output / 'tests'} -v")
    print(f"  4. Copy to ~/.local/share/luducat/plugins/{cfg['name']}/")


if __name__ == "__main__":
    main()
