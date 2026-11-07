# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""luducat - Cross-Platform Game Catalogue Browser"""

__all__ = ["__version__", "__author__"]
__author__ = "luducat"


# Lazy: importing the package must not pull in luducat.core ahead of
# main.py's faulthandler setup (PEP 562).
def __getattr__(name):
    if name == "__version__":
        from luducat.core.constants import APP_VERSION
        return APP_VERSION
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
