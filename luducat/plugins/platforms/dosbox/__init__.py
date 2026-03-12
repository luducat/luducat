# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""DOSBox Platform Provider Plugin"""

from .provider import DOSBoxProvider
from .game_detector import detect_dosbox_game
from .config_manager import DOSBoxConfigManager

__all__ = ["DOSBoxProvider", "detect_dosbox_game", "DOSBoxConfigManager"]
