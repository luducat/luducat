# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# detection.py

"""Shared detection dataclass for platform providers.

PlatformCandidate represents a detected association between a game and
a platform (DOSBox, ScummVM, etc.) with a confidence score.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class PlatformCandidate:
    """Detected platform association with confidence score.

    Score thresholds:
        >= 90: auto-assign platform
        50-89: show as compatible platform option
        < 50:  don't suggest
    """
    game_id: str
    platform: str           # "dosbox" | "scummvm"
    score: int              # 0-100
    source: str             # "user_saved", "gog_dosbox_flag", "igdb_platform",
                            #  "pcgw_engine", "game_id_seed", "tag_match",
                            #  "metadata", "title_heuristic"
    reason: str             # Human-readable for tooltip
    detection_hint: Optional[Dict] = field(default=None)

    # Threshold constants
    AUTO_ASSIGN = 90
    SUGGEST = 50
