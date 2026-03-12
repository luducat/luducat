# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""Template Store Plugin

Copy this directory and modify to create a new store plugin.
See store.py for implementation guide.
"""

from .store import TemplateStore

__all__ = ["TemplateStore"]
