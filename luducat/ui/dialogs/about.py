# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# about.py

"""About dialog for luducat"""

import sys
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from PySide6.QtCore import (
    QPropertyAnimation,
    QSequentialAnimationGroup,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ...core.constants import (
    APP_NAME,
    APP_VERSION_FULL,
    APP_DESCRIPTION,
    APP_HOMEPAGE,
    APP_AUTHOR,
    APP_LICENSE,
)
from ...core.news import get_news_html

if TYPE_CHECKING:
    from ...core.plugin_manager import PluginManager
    from ...core.update_checker import UpdateInfo


def _get_app_icon_path() -> Optional[Path]:
    """Get path to app icon, checking multiple locations"""
    package_dir = Path(__file__).parent.parent.parent
    icon_paths = [
        package_dir / "assets" / "appicons" / "app_icon_128x128.png",
        package_dir / "assets" / "appicons" / "app_icon_64x64.png",
    ]
    for path in icon_paths:
        if path.exists():
            return path
    return None


class AboutDialog(QDialog):
    """About dialog showing application information"""

    show_update_requested = Signal()

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        plugin_manager: Optional["PluginManager"] = None,
        update_info: Optional["UpdateInfo"] = None,
    ):
        super().__init__(parent)
        self.setObjectName("aboutDialog")
        self.setWindowTitle(_("About {name}").format(name=APP_NAME))
        self.setMinimumSize(550, 740)
        self.resize(550, 740)
        self.setModal(True)
        self._plugin_manager = plugin_manager
        self._update_info = update_info
        self._pulse_group: Optional[QSequentialAnimationGroup] = None
        self._pulse_stop_timer: Optional[QTimer] = None

        self._setup_ui()

        # Start pulse animation on update link (after UI built)
        if self._update_info and hasattr(self, "_update_link"):
            self._start_pulse_animation()

    def _setup_ui(self) -> None:
        """Create dialog layout"""
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Tab widget - store reference for select_news_tab()
        self._tabs = QTabWidget()
        self._tabs.addTab(self._create_about_tab(), _("About"))
        self._tabs.addTab(self._create_news_tab(), _("News"))
        self._tabs.addTab(self._create_credits_tab(), _("Credits"))
        self._tabs.addTab(self._create_development_tab(), _("Development"))
        layout.addWidget(self._tabs)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)

        homepage_btn = QPushButton(_("Homepage"))
        homepage_btn.clicked.connect(self._open_homepage)
        button_layout.addWidget(homepage_btn)

        button_layout.addStretch()

        close_btn = QPushButton(_("Close"))
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        button_layout.addWidget(close_btn)

        layout.addLayout(button_layout)

    def _create_about_tab(self) -> QWidget:
        """Create the main About tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Header with icon and info
        header = QHBoxLayout()
        header.setSpacing(16)
        header.setAlignment(Qt.AlignmentFlag.AlignTop)

        # App icon (128x128)
        icon_label = QLabel()
        icon_path = _get_app_icon_path()
        if icon_path:
            pixmap = QPixmap(str(icon_path))
            icon_label.setPixmap(pixmap.scaled(
                128, 128,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            ))
        else:
            icon_label.setText("🎮")
            icon_label.setObjectName("aboutIconFallback")
        icon_label.setFixedSize(128, 128)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(icon_label)

        # Info column (aligned with icon)
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        info_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Title + version row (with optional Update link)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_label = QLabel(f"{APP_NAME} v{APP_VERSION_FULL}")
        title_label.setObjectName("aboutTitle")
        title_row.addWidget(title_label)
        title_row.addStretch()

        if self._update_info:
            update_link = QLabel(
                "<a href='#update'>{}</a>".format(_("Update"))
            )
            update_link.setObjectName("aboutUpdateLink")
            update_link.setTextFormat(Qt.TextFormat.RichText)
            update_link.setCursor(Qt.CursorShape.PointingHandCursor)
            update_link.linkActivated.connect(self._on_update_clicked)

            # Tooltip with summary
            tip = _("Version {ver} available — click to update").format(
                ver=self._update_info.version
            )
            if self._update_info.changelog:
                from ...core.news import format_summary_text
                tip = (
                    _("Version {ver}").format(ver=self._update_info.version)
                    + "\n\n"
                    + format_summary_text(self._update_info.changelog)
                )
            update_link.setToolTip(tip)
            title_row.addWidget(update_link)

            # Store reference for pulse animation
            self._update_link = update_link

        info_layout.addLayout(title_row)

        # Description
        desc_label = QLabel(_(APP_DESCRIPTION))
        desc_label.setObjectName("aboutSubtitle")
        desc_label.setWordWrap(True)
        info_layout.addWidget(desc_label)

        # Author email (mailto link)
        author_label = QLabel(_("by {author}").format(
            author=f"<a href='mailto:{APP_AUTHOR}'>{APP_AUTHOR}</a>"
        ))
        author_label.setObjectName("aboutLink")
        author_label.linkActivated.connect(self._on_link_activated)
        info_layout.addWidget(author_label)

        # Homepage link
        homepage_label = QLabel(f"<a href='{APP_HOMEPAGE}'>{APP_HOMEPAGE}</a>")
        homepage_label.setObjectName("aboutLink")
        homepage_label.linkActivated.connect(self._on_link_activated)
        info_layout.addWidget(homepage_label)

        header.addLayout(info_layout)
        header.addStretch()
        layout.addLayout(header)

        # Description box with scroll area
        desc_frame = QFrame()
        desc_frame.setObjectName("aboutDescBox")
        desc_frame_layout = QVBoxLayout(desc_frame)
        desc_frame_layout.setContentsMargins(0, 0, 0, 0)

        desc_scroll = QScrollArea()
        desc_scroll.setObjectName("aboutDescScroll")
        desc_scroll.setWidgetResizable(True)
        desc_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        desc_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        desc_scroll.setMaximumHeight(200)

        desc_content = QWidget()
        desc_content_layout = QVBoxLayout(desc_content)
        desc_content_layout.setContentsMargins(12, 10, 12, 10)

        description_text = (
            _("{name} is a game catalogue browser that brings your Steam, GOG, "
              "and Epic libraries together in one place. Everything stays on "
              "your machine — no cloud, no accounts, no telemetry. After the "
              "first sync, it works entirely offline.").format(
                name=APP_NAME
            ) + "\n\n"

            + _("Browse your collection with pretty covers, or a "
                "screenshot mosaic. Open any game to see its full description, "
                "rich metadata, everything {name} knows about "
                "it. Pick a theme, adjust the zoom, and make it yours.").format(
                name=APP_NAME
            ) + "\n\n"

            + _("Metadata arrives automatically — cover art, genres, ratings, "
                "compatibility info, game modes — so you spend time browsing, "
                "not cataloguing. When you find something to play, {name} "
                "hands it off to the right launcher.").format(
                name=APP_NAME
            ) + "\n\n"

            + _("Built for anyone who looked at their library one day and "
                "thought \"I own how many games?\" — tens, hundreds, thousands? "
                "{name} handles that with ease.").format(
                name=APP_NAME
            )
        )
        description_label = QLabel(description_text)
        description_label.setObjectName("aboutDescText")
        description_label.setWordWrap(True)
        desc_content_layout.addWidget(description_label)

        desc_scroll.setWidget(desc_content)
        desc_frame_layout.addWidget(desc_scroll)
        layout.addWidget(desc_frame)

        # Plugins box
        plugins_frame = QFrame()
        plugins_frame.setObjectName("aboutPluginsBox")
        plugins_layout = QVBoxLayout(plugins_frame)
        plugins_layout.setContentsMargins(12, 10, 12, 10)
        plugins_layout.setSpacing(4)

        plugins_header = QLabel(_("Installed Plugins:"))
        plugins_header.setObjectName("aboutBoxHeader")
        plugins_layout.addWidget(plugins_header)

        # Plugin list (scrollable)
        plugins_scroll = QScrollArea()
        plugins_scroll.setObjectName("aboutPluginsScroll")
        plugins_scroll.setWidgetResizable(True)
        plugins_scroll.setMaximumHeight(140)
        plugins_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        plugins_content = QWidget()
        plugins_content_layout = QVBoxLayout(plugins_content)
        plugins_content_layout.setContentsMargins(0, 0, 0, 0)
        plugins_content_layout.setSpacing(2)

        if self._plugin_manager:
            discovered = self._plugin_manager.get_discovered_plugins()
            if discovered:
                for name, meta in sorted(discovered.items()):
                    plugin_text = f"• {meta.display_name} v{meta.version}"
                    plugin_label = QLabel(plugin_text)
                    plugin_label.setObjectName("aboutPluginItem")
                    plugins_content_layout.addWidget(plugin_label)
            else:
                no_plugins = QLabel(_("No plugins installed"))
                no_plugins.setObjectName("aboutPluginItem")
                plugins_content_layout.addWidget(no_plugins)
        else:
            no_plugins = QLabel(_("Plugin information not available"))
            no_plugins.setObjectName("aboutPluginItem")
            plugins_content_layout.addWidget(no_plugins)

        plugins_content_layout.addStretch()
        plugins_scroll.setWidget(plugins_content)
        plugins_layout.addWidget(plugins_scroll)
        layout.addWidget(plugins_frame)

        # License box at bottom
        license_frame = QFrame()
        license_frame.setObjectName("aboutLicenseBox")
        license_layout = QVBoxLayout(license_frame)
        license_layout.setContentsMargins(12, 10, 12, 10)
        license_layout.setSpacing(12)

        # Main license
        license_label = QLabel(_("Licensed under the {license}").format(
            license=APP_LICENSE
        ))
        license_label.setObjectName("aboutLicenseText")
        license_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        license_layout.addWidget(license_label)

        # Required third-party license attributions (GPL, LGPL, Apache)
        attribution_text = _(
            "Contains code adapted from "
            "<a href='https://github.com/derrod/legendary'>Legendary</a> "
            "(<a href='https://www.gnu.org/licenses/gpl-3.0.html'>GPLv3+</a>). "
            "Uses components licensed under "
            "<a href='https://www.gnu.org/licenses/lgpl-3.0.html'>LGPL v3</a> "
            "(Qt/PySide6, browser_cookie3) and "
            "<a href='https://www.apache.org/licenses/LICENSE-2.0'>Apache 2.0</a> "
            "(requests, aiohttp, orjson). See Credits tab for full attribution."
        )
        attribution_label = QLabel(attribution_text)
        attribution_label.setObjectName("aboutLicenseText")  # Reuse same style
        attribution_label.setWordWrap(True)
        attribution_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        attribution_label.linkActivated.connect(self._on_link_activated)
        license_layout.addWidget(attribution_label)

        layout.addWidget(license_frame)

        return widget

    def _create_news_tab(self) -> QWidget:
        """Create the News/Changelog tab with user-friendly release notes"""
        browser = QTextBrowser()
        browser.setObjectName("aboutNewsBrowser")
        browser.setOpenExternalLinks(False)

        # Build HTML content from news entries
        html = get_news_html()
        browser.setHtml(html)

        return browser

    def _create_credits_tab(self) -> QWidget:
        """Create the Credits tab with third-party attributions"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(8)
        layout.setContentsMargins(16, 16, 16, 16)

        # Data Sources
        layout.addWidget(self._create_section_header(_("Data Sources")))
        layout.addWidget(self._create_credit_item(
            "Steam Web API",
            "https://steamcommunity.com/dev",
            _("Game library and metadata from Valve's Steam platform."),
            "Steam API Terms",
            "https://steamcommunity.com/dev/apiterms",
        ))
        layout.addWidget(self._create_credit_item(
            "GOGdb",
            "https://www.gogdb.org",
            _("GOG game database dumps. Maintained by Yepoleb."),
            "MIT",
            "https://github.com/Yepoleb/gogdb/blob/master/LICENSE",
        ))
        layout.addWidget(self._create_credit_item(
            "Epic Games Store",
            "https://store.epicgames.com",
            _("Game library and metadata via direct Epic API."),
        ))
        layout.addWidget(self._create_credit_item(
            "IGDB",
            "https://www.igdb.com",
            _("Game metadata database by Twitch/Amazon."),
            "IGDB Terms",
            "https://www.igdb.com/api-terms-of-service",
        ))
        layout.addWidget(self._create_credit_item(
            "SteamGridDB",
            "https://www.steamgriddb.com",
            _("Community game artwork: covers, heroes, logos, icons."),
        ))
        layout.addWidget(self._create_credit_item(
            "ProtonDB",
            "https://www.protondb.com",
            _("Linux and Steam Deck compatibility reports."),
        ))
        layout.addWidget(self._create_credit_item(
            "PCGamingWiki",
            "https://www.pcgamingwiki.com",
            _("Game mode data: multiplayer, co-op, split screen."),
            "CC BY-SA 3.0",
            "https://creativecommons.org/licenses/by-sa/3.0/",
        ))

        # Adapted Code (GPL-licensed source adaptations)
        layout.addWidget(self._create_section_header(_("Adapted Code (GPLv3)")))
        layout.addWidget(self._create_credit_item(
            "Legendary",
            "https://github.com/derrod/legendary",
            _("Epic Games API client and OAuth session management adapted "
              "from Legendary by derrod. Used in the Epic store plugin."),
            "GPLv3+",
            "https://github.com/derrod/legendary/blob/master/LICENSE",
        ))

        # Core Frameworks (LGPL - require attribution and source availability)
        layout.addWidget(self._create_section_header(_("Core Frameworks (LGPL)")))
        layout.addWidget(self._create_credit_item(
            "Qt / PySide6",
            "https://www.qt.io",
            _("Cross-platform UI framework.") + "\nSource: https://code.qt.io",
            "LGPL v3",
            "https://www.gnu.org/licenses/lgpl-3.0.html",
        ))
        layout.addWidget(self._create_credit_item(
            "browser_cookie3",
            "https://github.com/borisbabic/browser_cookie3",
            _("Browser cookie extraction library."),
            "LGPL v3",
            "https://github.com/borisbabic/browser_cookie3/blob/master/LICENSE",
        ))

        # Python
        layout.addWidget(self._create_section_header(_("Runtime")))
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        layout.addWidget(self._create_credit_item(
            f"Python {python_version}",
            "https://www.python.org",
            _("Programming language runtime."),
            "PSF License",
            "https://docs.python.org/3/license.html",
        ))

        # MIT Licensed Libraries
        layout.addWidget(self._create_section_header(_("Libraries (MIT License)")))
        mit_libs = [
            ("SQLAlchemy", "https://www.sqlalchemy.org",
             _("SQL toolkit and ORM."),
             "https://github.com/sqlalchemy/sqlalchemy/blob/main/LICENSE"),
            ("Alembic", "https://alembic.sqlalchemy.org",
             _("Database migrations."),
             "https://github.com/sqlalchemy/alembic/blob/main/LICENSE"),
            ("platformdirs", "https://github.com/platformdirs/platformdirs",
             _("Platform-specific directories."),
             "https://github.com/platformdirs/platformdirs/blob/main/LICENSE"),
            ("tomli-w", "https://github.com/hukkin/tomli",
             _("TOML writer."),
             "https://github.com/hukkin/tomli/blob/master/LICENSE"),
            ("keyring", "https://github.com/jaraco/keyring",
             _("Secure credential storage."),
             "https://github.com/jaraco/keyring/blob/main/LICENSE"),
            ("beautifulsoup4", "https://www.crummy.com/software/BeautifulSoup",
             _("HTML/XML parser."),
             "https://git.launchpad.net/beautifulsoup/tree/LICENSE"),
            ("vdf", "https://github.com/ValvePython/vdf",
             _("Valve Data Format parser."),
             "https://github.com/ValvePython/vdf/blob/master/LICENSE"),
            ("urllib3", "https://github.com/urllib3/urllib3",
             _("HTTP client with connection pooling."),
             "https://github.com/urllib3/urllib3/blob/main/LICENSE.txt"),
        ]
        for name, url, desc, lic_url in mit_libs:
            layout.addWidget(self._create_credit_item(name, url, desc, "MIT", lic_url))

        # Apache Licensed Libraries
        layout.addWidget(self._create_section_header(_("Libraries (Apache 2.0 License)")))
        apache_libs = [
            ("requests", "https://requests.readthedocs.io",
             _("HTTP library for Python."),
             "https://github.com/psf/requests/blob/main/LICENSE"),
            ("aiohttp", "https://aiohttp.readthedocs.io",
             _("Async HTTP client/server."),
             "https://github.com/aio-libs/aiohttp/blob/master/LICENSE.txt"),
            ("orjson", "https://github.com/ijl/orjson",
             _("Fast JSON serialization."),
             "https://github.com/ijl/orjson/blob/master/LICENSE-APACHE"),
            ("oschmod", "https://github.com/YakDriver/oschmod",
             _("Cross-platform file permissions."),
             "https://github.com/YakDriver/oschmod/blob/main/LICENSE"),
        ]
        for name, url, desc, lic_url in apache_libs:
            layout.addWidget(self._create_credit_item(name, url, desc, "Apache 2.0", lic_url))

        # BSD Licensed Libraries
        layout.addWidget(self._create_section_header(_("Libraries (BSD License)")))
        bsd_libs = [
            ("python-dateutil", "https://github.com/dateutil/dateutil",
             _("Date and time utilities."),
             "BSD-3-Clause / Apache 2.0",
             "https://github.com/dateutil/dateutil/blob/master/LICENSE"),
            ("packaging", "https://github.com/pypa/packaging",
             _("Version parsing utilities."),
             "BSD-2-Clause / Apache 2.0",
             "https://github.com/pypa/packaging/blob/main/LICENSE.BSD"),
            ("Pillow", "https://python-pillow.org",
             _("Image processing library."),
             "HPND",
             "https://github.com/python-pillow/Pillow/blob/main/LICENSE"),
            ("pillow-heif", "https://github.com/bigcat88/pillow_heif",
             _("HEIF/HEIC image support."),
             "BSD-3-Clause",
             "https://github.com/bigcat88/pillow_heif/blob/master/LICENSE.txt"),
            ("psutil", "https://github.com/giampaolo/psutil",
             _("Process and system utilities."),
             "BSD-3-Clause",
             "https://github.com/giampaolo/psutil/blob/master/LICENSE"),
            ("SecretStorage", "https://github.com/mitya57/secretstorage",
             _("Linux keyring backend."),
             "BSD-3-Clause",
             "https://github.com/mitya57/secretstorage/blob/master/LICENSE"),
        ]
        for name, url, desc, lic_name, lic_url in bsd_libs:
            layout.addWidget(self._create_credit_item(name, url, desc, lic_name, lic_url))

        # Bundled Tools (Windows/WINE)
        layout.addWidget(self._create_section_header(_("Bundled Tools (Windows/WINE)")))
        layout.addWidget(self._create_credit_item(
            "curl",
            "https://curl.se",
            _("Command-line HTTP client."),
            "curl (MIT/X derivative)",
            "https://curl.se/docs/copyright.html",
        ))
        layout.addWidget(self._create_credit_item(
            "7-Zip (7za)",
            "https://www.7-zip.org",
            _("Archive extraction tool."),
            "LGPL 2.1+",
            "https://www.7-zip.org/license.txt",
        ))

        layout.addStretch()
        scroll.setWidget(widget)
        return scroll

    def _create_development_tab(self) -> QWidget:
        """Create the Development/AI Disclosure tab"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # AI Disclosure
        layout.addWidget(self._create_section_header(_("AI-Assisted Development")))

        disclosure_text = (
            _("This disclosure is provided in the interest of transparency about "
              "modern software development practices.") + "\n\n"
            + _("This project was architected, planned, debugged, tested, reviewed and "
                "designed by humans with tool-assisted code generation.") + "\n\n"
            + _("All code contributions were manually reviewed, debugged and approved, "
                "as well as building and collecting necessary information for "
                "application use.") + "\n\n"
        )
        disclosure_label = QLabel(disclosure_text)
        disclosure_label.setObjectName("aboutBodyText")
        disclosure_label.setWordWrap(True)
        layout.addWidget(disclosure_label)

        layout.addStretch()
        scroll.setWidget(widget)
        return scroll

    def _create_section_header(self, text: str) -> QLabel:
        """Create a section header label"""
        label = QLabel(text)
        label.setObjectName("aboutSectionHeader")
        return label

    def _create_credit_item(
        self,
        name: str,
        url: str,
        description: str,
        license_name: str = "",
        license_url: str = "",
    ) -> QWidget:
        """Create a credit item with name, URL, description, and license link"""
        widget = QWidget()
        widget.setObjectName("creditItem")
        layout = QVBoxLayout(widget)
        layout.setSpacing(2)
        layout.setContentsMargins(8, 6, 8, 6)

        # Name as clickable link
        name_label = QLabel(f"<a href='{url}'>{name}</a>")
        name_label.setObjectName("creditItemName")
        name_label.linkActivated.connect(self._on_link_activated)
        layout.addWidget(name_label)

        # Description
        desc_label = QLabel(description)
        desc_label.setObjectName("creditItemDesc")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        # License link (if provided)
        if license_name and license_url:
            license_label = QLabel(_("License: {link}").format(
                link=f"<a href='{license_url}'>{license_name}</a>"
            ))
            license_label.setObjectName("creditItemLicense")
            license_label.linkActivated.connect(self._on_link_activated)
            layout.addWidget(license_label)

        return widget

    @staticmethod
    def _on_link_activated(url: str) -> None:
        """Open a clicked link in the user's preferred browser."""
        from ...utils.browser import open_url
        open_url(url)

    def _open_homepage(self) -> None:
        """Open project homepage in browser"""
        from ...utils.browser import open_url
        open_url(APP_HOMEPAGE)

    def select_news_tab(self) -> None:
        """Select the News tab programmatically.

        Used when auto-showing the dialog on version change to direct
        user attention to the changelog.
        """
        self._tabs.setCurrentIndex(1)  # News is second tab (index 1)

    # --- Update link animation ---

    def _start_pulse_animation(self) -> None:
        """Start a pulsating opacity animation on the update link."""
        from PySide6.QtCore import QEasingCurve

        opacity = QGraphicsOpacityEffect(self._update_link)
        opacity.setOpacity(1.0)
        self._update_link.setGraphicsEffect(opacity)

        fade_out = QPropertyAnimation(opacity, b"opacity")
        fade_out.setDuration(800)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.3)
        fade_out.setEasingCurve(QEasingCurve.Type.InOutSine)

        fade_in = QPropertyAnimation(opacity, b"opacity")
        fade_in.setDuration(800)
        fade_in.setStartValue(0.3)
        fade_in.setEndValue(1.0)
        fade_in.setEasingCurve(QEasingCurve.Type.InOutSine)

        self._pulse_group = QSequentialAnimationGroup(self)
        self._pulse_group.addAnimation(fade_out)
        self._pulse_group.addAnimation(fade_in)
        self._pulse_group.setLoopCount(-1)
        self._pulse_group.start()

        # Stop pulsing after 15 seconds
        self._pulse_stop_timer = QTimer(self)
        self._pulse_stop_timer.setSingleShot(True)
        self._pulse_stop_timer.timeout.connect(self._stop_pulse_animation)
        self._pulse_stop_timer.start(15000)

    def _stop_pulse_animation(self) -> None:
        """Stop the pulse animation, ensuring full opacity."""
        if self._pulse_group:
            self._pulse_group.stop()
        effect = self._update_link.graphicsEffect()
        if isinstance(effect, QGraphicsOpacityEffect):
            effect.setOpacity(1.0)

    def _cleanup_animation(self) -> None:
        """Clean up animation resources safely."""
        if self._pulse_stop_timer:
            self._pulse_stop_timer.stop()
        if self._pulse_group:
            self._pulse_group.stop()
            self._pulse_group = None

    def _on_update_clicked(self, _link: str) -> None:
        """Close About dialog and request update dialog from MainWindow."""
        self._cleanup_animation()
        self.show_update_requested.emit()
        self.accept()

    def accept(self) -> None:
        self._cleanup_animation()
        super().accept()

    def reject(self) -> None:
        self._cleanup_animation()
        super().reject()
