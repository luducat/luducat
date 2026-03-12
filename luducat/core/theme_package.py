# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# theme_package.py

"""Theme package loader for luducat

Handles loading and processing .luducat-theme packages (ZIP archives)
and JSON variant files.

Package structure:
    mytheme.luducat-theme (ZIP)
    ├── theme.json          # Metadata
    ├── base.qss            # Template (optional if using bundled base)
    ├── preview.png         # Optional preview image
    └── variants/
        ├── default.json    # Default color scheme
        ├── light.json      # Optional variant
        └── custom.json     # User-created variant
"""

from luducat.core.json_compat import json
import logging
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Any

from .theme_variables import (
    VARIABLE_NAMES,
    DEFAULT_VALUES,
    process_template,
    generate_palette_block,
)

logger = logging.getLogger(__name__)

# Package format version
THEME_PACKAGE_FORMAT_VERSION = 1


class ThemeVariant:
    """Represents a color scheme variant for a theme."""

    def __init__(
        self,
        name: str,
        variables: Dict[str, str],
        description: str = "",
    ):
        self.name = name
        self.variables = variables
        self.description = description

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "ThemeVariant":
        """Create variant from JSON data."""
        name = data.get("name", "Unknown")
        description = data.get("description", "")

        # Extract color variables (everything except name/description)
        variables = {k: v for k, v in data.items() if k not in ("name", "description")}

        return cls(name=name, variables=variables, description=description)

    @classmethod
    def from_file(cls, path: Path) -> "ThemeVariant":
        """Load variant from JSON file."""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        variant = cls.from_json(data)
        # Use filename as name if not specified
        if variant.name == "Unknown":
            variant.name = path.stem.replace("_", " ").replace("-", " ").title()
        return variant

    def get_complete_variables(self) -> Dict[str, str]:
        """Get variables with defaults filled in for missing values.

        Derived variables (btn_gradient_top, btn_border_color) are computed
        from the theme's own values if not explicitly set, so buttons
        automatically match each theme without every JSON needing to
        specify them.
        """
        complete = dict(DEFAULT_VALUES)
        complete.update(self.variables)

        # Derived button variables — default to the theme's own colors
        if "btn_gradient_top" not in self.variables:
            complete["btn_gradient_top"] = complete["border"]
        if "btn_border_color" not in self.variables:
            complete["btn_border_color"] = complete["accent"]

        return complete


class ThemePackage:
    """Represents a .luducat-theme package."""

    def __init__(
        self,
        name: str,
        package_path: Optional[Path] = None,
        base_qss: Optional[str] = None,
        variants: Optional[Dict[str, ThemeVariant]] = None,
        default_variant: str = "default",
        author: str = "",
        description: str = "",
        version: str = "1.0.0",
        uses_bundled_base: bool = True,
        font_family: Optional[str] = None,
        assets: Optional[List[str]] = None,
    ):
        self.name = name
        self.package_path = package_path
        self.base_qss = base_qss
        self.variants = variants or {}
        self.default_variant = default_variant
        self.author = author
        self.description = description
        self.version = version
        self.uses_bundled_base = uses_bundled_base
        self.font_family = font_family
        self.assets = assets or []

        # Cache for generated QSS
        self._qss_cache: Dict[str, str] = {}

    def get_variant_names(self) -> List[str]:
        """Get list of available variant names."""
        return list(self.variants.keys())

    def get_variant(self, name: str) -> Optional[ThemeVariant]:
        """Get a specific variant by name."""
        return self.variants.get(name)

    def generate_qss(self, variant_name: Optional[str] = None) -> str:
        """Generate QSS from template and variant.

        Args:
            variant_name: Variant to use (default if not specified)

        Returns:
            Processed QSS stylesheet
        """
        if variant_name is None:
            variant_name = self.default_variant

        # Check cache
        cache_key = variant_name
        if cache_key in self._qss_cache:
            return self._qss_cache[cache_key]

        # Get variant
        variant = self.variants.get(variant_name)
        if not variant:
            logger.warning(f"Variant '{variant_name}' not found, using default")
            variant = self.variants.get(self.default_variant)
            if not variant:
                # Fallback to first available variant
                if self.variants:
                    variant = next(iter(self.variants.values()))
                else:
                    # No variants - use defaults
                    variant = ThemeVariant("default", {})

        # Get complete variables with defaults
        variables = variant.get_complete_variables()

        # Override font family if specified at package level
        if self.font_family:
            variables["font_family"] = self.font_family

        # Get base template
        if self.base_qss:
            template = self.base_qss
        else:
            # Load bundled base template
            template = _get_bundled_base_template()

        # Generate palette block
        palette_block = generate_palette_block(variables)

        # Replace palette placeholder
        if "{{PALETTE_BLOCK}}" in template:
            template = template.replace("{{PALETTE_BLOCK}}", palette_block)

        # Process template with variables
        qss = process_template(template, variables)

        # Cache result
        self._qss_cache[cache_key] = qss

        return qss

    def clear_cache(self):
        """Clear the QSS cache."""
        self._qss_cache.clear()


def _get_bundled_base_template() -> str:
    """Load the bundled base.qss template."""
    base_path = Path(__file__).parent.parent / "assets" / "themes" / "base.qss"
    if base_path.exists():
        with open(base_path, "r", encoding="utf-8") as f:
            return f.read()
    else:
        logger.error(f"Bundled base template not found: {base_path}")
        return ""


def load_theme_package(path: Path) -> Optional[ThemePackage]:
    """Load a .luducat-theme package.

    Args:
        path: Path to .luducat-theme file (ZIP archive)

    Returns:
        ThemePackage instance, or None if loading failed
    """
    if not path.exists():
        logger.error(f"Theme package not found: {path}")
        return None

    try:
        with zipfile.ZipFile(path, "r") as zf:
            # Read theme.json metadata
            try:
                with zf.open("theme.json") as f:
                    metadata = json.load(f)
            except KeyError:
                logger.error(f"theme.json not found in package: {path}")
                return None

            # Extract metadata
            name = metadata.get("name", path.stem)
            author = metadata.get("author", "")
            description = metadata.get("description", "")
            version = metadata.get("version", "1.0.0")
            default_variant = metadata.get("default_variant", "default")
            uses_bundled_base = metadata.get("uses_bundled_base", True)
            font_family = metadata.get("font_family")
            assets = metadata.get("assets", [])

            # Load base.qss if present
            base_qss = None
            if not uses_bundled_base:
                try:
                    with zf.open("base.qss") as f:
                        base_qss = f.read().decode("utf-8")
                except KeyError:
                    logger.warning(f"base.qss not found in package, using bundled: {path}")

            # Load variants
            variants = {}
            for name_in_zip in zf.namelist():
                if name_in_zip.startswith("variants/") and name_in_zip.endswith(".json"):
                    variant_name = Path(name_in_zip).stem
                    try:
                        with zf.open(name_in_zip) as f:
                            variant_data = json.load(f)
                            variants[variant_name] = ThemeVariant.from_json(variant_data)
                    except Exception as e:
                        logger.warning(f"Failed to load variant {variant_name}: {e}")

            return ThemePackage(
                name=name,
                package_path=path,
                base_qss=base_qss,
                variants=variants,
                default_variant=default_variant,
                author=author,
                description=description,
                version=version,
                uses_bundled_base=uses_bundled_base,
                font_family=font_family,
                assets=assets,
            )

    except zipfile.BadZipFile:
        logger.error(f"Invalid ZIP file: {path}")
        return None
    except Exception as e:
        logger.error(f"Failed to load theme package {path}: {e}")
        return None


def load_variant_from_file(path: Path) -> Optional[ThemeVariant]:
    """Load a standalone variant JSON file.

    This is used for loading variant files from the variants/ directory
    without a full .luducat-theme package.

    Args:
        path: Path to variant JSON file

    Returns:
        ThemeVariant instance, or None if loading failed
    """
    if not path.exists():
        logger.error(f"Variant file not found: {path}")
        return None

    try:
        return ThemeVariant.from_file(path)
    except Exception as e:
        logger.error(f"Failed to load variant from {path}: {e}")
        return None


def create_variant_from_existing_qss(qss_path: Path) -> Optional[Dict[str, str]]:
    """Extract color variables from an existing QSS file.

    Attempts to parse color values from a QSS file and map them
    to theme variables. This is a helper for migrating existing
    themes to the new variant format.

    Args:
        qss_path: Path to QSS file

    Returns:
        Dict of variable_name -> color_value, or None if extraction failed
    """
    import re

    if not qss_path.exists():
        return None

    try:
        with open(qss_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Try to extract from @palette block first
        palette_match = re.search(r'/\*\s*@palette\s+(.*?)\*/', content, re.DOTALL)
        if palette_match:
            palette_text = palette_match.group(1)
            palette_colors = {}
            for line in palette_text.split('\n'):
                line = line.strip()
                if '=' in line:
                    role, color = line.split('=', 1)
                    palette_colors[role.strip()] = color.strip()

            # Map palette roles to our variables
            variables = {}

            # Background colors
            if "Window" in palette_colors:
                variables["bg_primary"] = palette_colors["Window"]
            if "AlternateBase" in palette_colors:
                variables["bg_secondary"] = palette_colors["AlternateBase"]
            if "Base" in palette_colors:
                variables["bg_input"] = palette_colors["Base"]
            if "Midlight" in palette_colors:
                variables["bg_hover"] = palette_colors["Midlight"]

            # Text colors
            if "WindowText" in palette_colors:
                variables["text_primary"] = palette_colors["WindowText"]
            if "HighlightedText" in palette_colors:
                variables["text_on_accent"] = palette_colors["HighlightedText"]

            # Accent colors
            if "Highlight" in palette_colors:
                variables["accent"] = palette_colors["Highlight"]
            if "Link" in palette_colors:
                variables["link_color"] = palette_colors["Link"]

            # Border
            if "Mid" in palette_colors:
                variables["border"] = palette_colors["Mid"]

            # Light
            if "Light" in palette_colors:
                variables["light"] = palette_colors["Light"]

            return variables

    except Exception as e:
        logger.error(f"Failed to extract colors from {qss_path}: {e}")

    return None


def generate_qss_from_variant(variant_path: Path, base_path: Optional[Path] = None) -> str:
    """Generate QSS from a variant file and optional base template.

    Convenience function for quick QSS generation without creating
    a full ThemePackage.

    Args:
        variant_path: Path to variant JSON file
        base_path: Path to base.qss template (uses bundled if not specified)

    Returns:
        Generated QSS string
    """
    variant = load_variant_from_file(variant_path)
    if not variant:
        return ""

    # Load base template
    if base_path and base_path.exists():
        with open(base_path, "r", encoding="utf-8") as f:
            template = f.read()
    else:
        template = _get_bundled_base_template()

    # Get complete variables
    variables = variant.get_complete_variables()

    # Generate palette block
    palette_block = generate_palette_block(variables)

    # Replace palette placeholder
    if "{{PALETTE_BLOCK}}" in template:
        template = template.replace("{{PALETTE_BLOCK}}", palette_block)

    # Process template
    return process_template(template, variables)
