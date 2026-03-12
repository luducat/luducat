# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# theme_variables.py

"""Theme variable definitions for luducat

This module defines the color variables used in theme templates.
Templates use {{variable_name}} placeholders that get replaced with
actual color values from variant JSON files.

Variable Naming Convention:
- bg_*: Background colors
- text_*: Text colors
- accent_*: Accent/highlight colors
- border: Border/separator color
- scrollbar_*: Scrollbar colors
- fav_*: Favorite button colors (gold/yellow)
"""

from typing import Dict, List, Tuple


# Core theme variables with descriptions
# Format: (variable_name, description, default_value)
THEME_VARIABLES: List[Tuple[str, str, str]] = [
    # === Background Colors ===
    ("bg_primary", "Main window/dialog background", "#171A21"),
    ("bg_secondary", "Panel/card/frame background", "#1B2838"),
    ("bg_input", "Input field and list background", "#16202D"),
    ("bg_hover", "Hover state background", "#1F364D"),

    # === Text Colors ===
    ("text_primary", "Primary text color", "#C7D5E0"),
    ("text_secondary", "Muted/hint text color", "#8BA4B8"),
    ("text_on_accent", "Text on accent/highlighted background", "#171A21"),

    # === Accent Colors (for gradients: bright -> hover -> base -> dark) ===
    ("accent", "Primary accent color (buttons, links, highlights)", "#30BFAF"),
    ("accent_hover", "Lighter accent for hover states", "#54D3C3"),
    ("accent_dark", "Darker accent for pressed/gradient bottom", "#249486"),
    ("accent_bright", "Brightest accent for highlight effects", "#6BE0CF"),

    # === UI Element Colors ===
    ("border", "Border/divider color", "#2A475E"),
    ("selection", "Selection/highlight background", "#2A475E"),

    # === Scrollbar Colors ===
    ("scrollbar_bg", "Scrollbar track background", "#171A21"),
    ("scrollbar_handle", "Scrollbar handle color", "#2A475E"),

    # === Favorite Button Colors (Gold/Yellow) ===
    ("fav_color", "Favorite button border and text color", "#f1c40f"),
    ("fav_checked", "Favorite button checked background", "#E8B80D"),
    ("fav_checked_hover", "Favorite button checked hover", "#F5CF3A"),
    ("fav_checked_dark", "Favorite button gradient dark", "#B8920A"),
    ("fav_hover_alpha", "Favorite hover transparent (rgba)", "rgba(241, 196, 15, 55%)"),
    ("fav_hover_alpha_mid", "Favorite hover transparent mid", "rgba(241, 196, 15, 30%)"),

    # === Tag/Badge Colors ===
    ("tag_bg", "Tag chip background", "#2A475E"),
    ("tag_border", "Tag chip border", "#30BFAF"),
    ("tag_text", "Tag chip text color", "#C7D5E0"),
    ("tag_hover_bg", "Tag chip hover background", "#1F364D"),
    ("badge_bg", "Game mode badge background", "palette(mid)"),
    ("badge_text", "Game mode badge text color", "palette(window-text)"),

    # === Favorite Star Color (used by view delegates for painted stars) ===
    ("fav_star_color", "Favorite star color in game list/grid views", "#f1c40f"),

    # === Download Status Colors ===
    ("download_completed", "Download progress bar color for completed status", "#4caf50"),
    ("download_failed", "Download progress bar color for failed status", "#f44336"),
    ("download_paused", "Download progress bar color for paused status", "#ff9800"),

    # === Score Colors (tag score, author score dialogs) ===
    ("score_positive", "Positive score tint color (green)", "#28b43c"),
    ("score_negative", "Negative score tint color (red)", "#c83232"),

    # === Special Colors ===
    ("title_color", "Game title color (often white or accent)", "#FFFFFF"),
    ("link_color", "Link color", "#30BFAF"),
    ("light", "Light color for subtle accents", "#3B5A78"),
    ("btn_gradient_top", "Button gradient top color", "#3B5A78"),
    ("btn_border_color", "Button border color (use 'transparent' for borderless)", "transparent"),
    ("license_text_color", "License text color in about dialog", "#C5D5E5"),

    # === Font (special - can be overridden per theme) ===
    ("font_family", "Font family stack", '-apple-system, BlinkMacSystemFont, "Segoe UI Variable", "Segoe UI", "Inter", "Helvetica Neue", Helvetica, Arial, sans-serif'),
]

# Variable names only (for quick lookup)
VARIABLE_NAMES = [var[0] for var in THEME_VARIABLES]

# Default values as a dict
DEFAULT_VALUES: Dict[str, str] = {var[0]: var[2] for var in THEME_VARIABLES}


def get_variable_descriptions() -> Dict[str, str]:
    """Get variable names mapped to their descriptions."""
    return {var[0]: var[1] for var in THEME_VARIABLES}


def validate_variant(variant: Dict[str, str]) -> List[str]:
    """Validate that a variant dict has all required variables.

    Args:
        variant: Dict of variable_name -> color_value

    Returns:
        List of missing variable names (empty if valid)
    """
    missing = []
    for name in VARIABLE_NAMES:
        if name not in variant:
            missing.append(name)
    return missing


def process_template(template_qss: str, variables: Dict[str, str]) -> str:
    """Replace {{variable}} placeholders with actual color values.

    Args:
        template_qss: QSS template with {{variable}} placeholders
        variables: Dict mapping variable names to color values

    Returns:
        Processed QSS with placeholders replaced
    """
    result = template_qss
    for var_name, value in variables.items():
        placeholder = "{{" + var_name + "}}"
        result = result.replace(placeholder, value)
    return result



def generate_palette_block(variables: Dict[str, str]) -> str:
    """Generate @palette block from theme variables.

    Args:
        variables: Dict mapping variable names to color values

    Returns:
        String containing /* @palette ... */ block
    """
    lines = ["/* @palette"]

    # Build palette mappings
    # Note: Some Qt roles share the same variable
    palette_entries = {
        "Window": variables.get("bg_primary", DEFAULT_VALUES["bg_primary"]),
        "WindowText": variables.get("text_primary", DEFAULT_VALUES["text_primary"]),
        "Base": variables.get("bg_input", DEFAULT_VALUES["bg_input"]),
        "AlternateBase": variables.get("bg_secondary", DEFAULT_VALUES["bg_secondary"]),
        "Text": variables.get("text_primary", DEFAULT_VALUES["text_primary"]),
        "Button": variables.get("border", DEFAULT_VALUES["border"]),
        "ButtonText": variables.get("text_primary", DEFAULT_VALUES["text_primary"]),
        "Highlight": variables.get("accent", DEFAULT_VALUES["accent"]),
        "HighlightedText": variables.get("text_on_accent", DEFAULT_VALUES["text_on_accent"]),
        "Link": variables.get("link_color", DEFAULT_VALUES["link_color"]),
        "Mid": variables.get("border", DEFAULT_VALUES["border"]),
        "Midlight": variables.get("bg_hover", DEFAULT_VALUES["bg_hover"]),
        "Dark": variables.get("bg_primary", DEFAULT_VALUES["bg_primary"]),
        "Light": variables.get("light", DEFAULT_VALUES["light"]),
    }

    for role, color in palette_entries.items():
        lines.append(f"   {role}={color}")

    lines.append("*/")
    return "\n".join(lines)
