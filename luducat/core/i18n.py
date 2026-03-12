# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# i18n.py

"""Internationalization (i18n) support for luducat

Uses Python gettext with .po/.mo files.
Language selection: config → system locale → English fallback.
Logging always stays in English.
"""

import builtins
import gettext
import locale
import logging
import os
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)

# The language code currently active (set by init_i18n)
_current_language: str = "en"

# Locale directory: luducat/assets/locale/{lang}/LC_MESSAGES/luducat.mo
LOCALE_DIR = Path(__file__).resolve().parent.parent / "assets" / "locale"

# Domain name for gettext
DOMAIN = "luducat"

# All languages the project supports (ordered for UI display).
# Only languages with a compiled .mo file are shown in the UI picker.
_ALL_LANGUAGES = OrderedDict([
    ("en", "English"),
    ("de", "Deutsch"),
    ("es", "Español"),
    ("pt_BR", "Português (Brasil)"),
    ("pt_PT", "Português (Portugal)"),
    ("fr", "Français"),
    ("it", "Italiano"),
])


def get_current_language() -> str:
    """Return the language code that is currently active."""
    return _current_language


def get_available_languages() -> OrderedDict:
    """Return languages that have a compiled .mo file present.

    English is always available (it's the source language / fallback).
    """
    available = OrderedDict()
    available["en"] = _ALL_LANGUAGES["en"]

    for lang, name in _ALL_LANGUAGES.items():
        if lang == "en":
            continue
        mo_path = LOCALE_DIR / lang / "LC_MESSAGES" / f"{DOMAIN}.mo"
        if mo_path.exists():
            available[lang] = name

    return available


def _detect_system_language() -> str:
    """Detect the system language from locale settings.

    Returns a language code from _ALL_LANGUAGES, or "en" as fallback.
    """
    try:
        sys_locale = locale.getlocale()[0] or locale.getdefaultlocale()[0] or ""
    except Exception:
        return "en"

    if not sys_locale:
        return "en"

    # Try exact match first (e.g. "pt_BR")
    if sys_locale in _ALL_LANGUAGES:
        return sys_locale

    # Try language-only match (e.g. "de_AT" → "de", "fr_CA" → "fr")
    lang_only = sys_locale.split("_")[0]
    if lang_only in _ALL_LANGUAGES:
        return lang_only

    # Special case: "pt" without region defaults to pt_BR
    if lang_only == "pt":
        return "pt_BR"

    return "en"


def init_i18n(language: str = "") -> str:
    """Initialize internationalization.

    Installs _(), N_(), and ngettext() into builtins so they are
    available everywhere without imports.

    Args:
        language: Language code ("en", "de", "pt_BR", etc.).
                  Empty string = auto-detect from system locale.

    Returns:
        The language code that was actually activated.
    """
    if not language:
        language = _detect_system_language()

    global _current_language

    # English = source language, no .mo needed
    if language == "en":
        builtins._ = lambda s: s
        builtins.N_ = lambda s: s
        builtins.ngettext = lambda singular, plural, n: singular if n == 1 else plural
        logger.info("i18n: using English (source language)")
        _current_language = "en"
        return "en"

    # Try to load the .mo file for the requested language
    try:
        translation = gettext.translation(
            DOMAIN,
            localedir=str(LOCALE_DIR),
            languages=[language],
            fallback=True,
        )
        builtins._ = translation.gettext
        builtins.N_ = lambda s: s  # extraction marker, identity at runtime
        builtins.ngettext = translation.ngettext
        logger.info(f"i18n: loaded language '{language}'")
        _current_language = language
        return language
    except Exception as e:
        logger.warning(f"i18n: failed to load '{language}': {e}, falling back to English")
        builtins._ = lambda s: s
        builtins.N_ = lambda s: s
        builtins.ngettext = lambda singular, plural, n: singular if n == 1 else plural
        _current_language = "en"
        return "en"


def install_qt_translator(app) -> bool:
    """Load and install Qt's own translation for standard UI elements.

    This translates Qt standard button labels (Cancel → Abbrechen, Yes → Ja,
    etc.) using the qtbase_*.qm files bundled with PySide6. Must be called
    after QApplication has been created.

    The language used matches the one set via init_i18n() — either the user's
    luducat language setting or the auto-detected system locale.

    Args:
        app: The QApplication instance.

    Returns:
        True if a Qt translation was loaded, False for English / not found.
    """
    if _current_language == "en":
        return False

    try:
        import PySide6
        from PySide6.QtCore import QTranslator

        translations_dir = os.path.join(
            os.path.dirname(PySide6.__file__), "Qt", "translations"
        )

        # Try exact match first (e.g. "pt_BR")
        translator = QTranslator(app)
        if translator.load(f"qtbase_{_current_language}", translations_dir):
            app.installTranslator(translator)
            logger.info(f"i18n: installed Qt translations for '{_current_language}'")
            return True

        # Try language-prefix fallback (e.g. "de" for "de_AT")
        lang_prefix = _current_language.split("_")[0]
        if lang_prefix != _current_language:
            translator2 = QTranslator(app)
            if translator2.load(f"qtbase_{lang_prefix}", translations_dir):
                app.installTranslator(translator2)
                logger.info(
                    f"i18n: installed Qt translations for '{lang_prefix}'"
                    f" (from '{_current_language}')"
                )
                return True

        logger.warning(
            f"i18n: no Qt translations found for '{_current_language}'"
            f" in {translations_dir}"
        )
        return False
    except Exception as e:
        logger.warning(f"i18n: failed to install Qt translator: {e}")
        return False
