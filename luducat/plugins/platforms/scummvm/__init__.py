# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""ScummVM Platform Provider Plugin"""

from .provider import ScummVMProvider
from .game_detector import detect_scummvm_game, SCUMMVM_GAME_IDS

__all__ = ["ScummVMProvider", "detect_scummvm_game", "SCUMMVM_GAME_IDS"]
