# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""Shared detection infrastructure for platform providers."""

from .detection import PlatformCandidate
from .platform_query import PlatformDataQuery

__all__ = ["PlatformCandidate", "PlatformDataQuery"]
