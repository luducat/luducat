# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
#
"""Ruleset-driven download handler for declarative store_engine stores.

One handler instance per virtual store whose ruleset declares a
``downloads`` section, registered at startup exactly parallel to
GogDownloadHandler. All store knowledge lives in the ruleset JSON;
auth and HTTP go through the owning VirtualStore. See
store-downloads-trio-plan.md for the schema.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from luducat.core.archivist.types import (
    ArchiveRequest,
    ArchiveType,
    DownloadTarget,
    GameDownloadInfo,
)
from luducat.core.download_handlers import base as base_module
from luducat.core.download_handlers.base import (
    AbstractDownloadHandler,
    AuthStatus,
)
from luducat.core.download_selection import _parse_size_string

logger = logging.getLogger(__name__)


def register_declarative_handlers(plugin_manager) -> int:
    """Register one handler per virtual store that declares downloads.

    Called at startup (main_window) after plugins are loaded. Stores
    without a downloads section are skipped; a store whose section is
    unusable is logged and skipped so one bad ruleset cannot take the
    others down. Re-registration (plugin reload) is tolerated.

    Returns:
        Number of handlers newly registered.
    """
    from luducat.core.download_handlers import register

    engine = plugin_manager.get_plugin("store_engine")
    if engine is None or not getattr(engine, "enabled", True):
        return 0

    count = 0
    for store in engine.get_store_instances():
        if store._ruleset.raw.get("downloads") is None:
            continue
        if not store.get_setting("enable_downloads", False):
            logger.debug("Downloads disabled for %s, skipping handler",
                         store.store_name)
            continue
        try:
            register(DeclarativeDownloadHandler(store))
            count += 1
            logger.debug("Registered declarative download handler: %s",
                         store.store_name)
        except ValueError as e:
            # Either already registered (reload) or an unusable
            # downloads section — both non-fatal, latter worth a log.
            if "already registered" not in str(e):
                logger.warning("Skipping download handler for %s: %s",
                               store.store_name, e)
    return count


class DeclarativeDownloadHandler(AbstractDownloadHandler):
    """Download handler driven by a ruleset ``downloads`` section.

    The handler never parses store responses itself — extraction goes
    through the store_engine backends with the ``downloads.fields`` spec,
    the same engine the library and detail sections already use.
    """

    def __init__(self, virtual_store) -> None:
        """Args:
            virtual_store: The owning VirtualStore (auth, HTTP, ruleset).

        Raises:
            ValueError: If the ruleset has no usable downloads section —
                the registration site must skip such stores instead of
                constructing handlers for them.
        """
        raw = virtual_store._ruleset.raw
        downloads = raw.get("downloads")
        if downloads is None:
            raise ValueError(
                f"Ruleset for '{virtual_store.store_name}' has no "
                "downloads section"
            )
        if not downloads.get("files_url_template"):
            raise ValueError(
                f"Ruleset downloads section for '{virtual_store.store_name}' "
                "lacks files_url_template"
            )

        self._store = virtual_store
        self._downloads = downloads
        self._url_patterns = [
            re.compile(p) for p in downloads.get("url_page_patterns", [])
        ]

    @property
    def store_name(self) -> str:
        return self._store.store_name

    @property
    def display_name(self) -> str:
        return self._store.display_name

    @property
    def url_patterns(self) -> Sequence[re.Pattern]:
        return self._url_patterns

    @property
    def auth_failure_message(self) -> str:
        return f"Authentication required for {self.display_name}."

    def check_auth(self) -> AuthStatus:
        if self._store.is_authenticated():
            return AuthStatus(True)
        return AuthStatus(False, self.auth_failure_message)

    def resolve_url(self, url: str) -> DownloadTarget:
        candidate = None
        for pattern in self._url_patterns:
            m = pattern.search(url)
            if m:
                candidate = m.group(1) if m.groups() else None
                break
        if not candidate:
            raise ValueError(
                f"Could not resolve {self.display_name} URL: {url}")

        # URL may carry a slug while the DB is keyed by app_id (ZOOM
        # uses UUIDs) - fall back to a slug scan.
        game = self._store._db.get_game(candidate)
        if game is None:
            game = next(
                (g for g in self._store._db.get_all_games()
                 if g.get("slug") == candidate),
                None,
            )
        if game is None:
            raise ValueError(
                f"'{candidate}' is not in your {self.display_name} "
                "library (sync the store first)"
            )

        info = self.get_available_downloads(game.get("app_id", candidate))

        # Drop-path auto-select, same semantics as the GOG handler:
        # native-platform installers (all installers when none match)
        # plus extras; patches stay unselected.
        current_os = base_module.detect_platform()
        platform_installers = [
            i for i in info.installers if i.get("platform") == current_os
        ]
        if not platform_installers:
            platform_installers = info.installers

        return self.resolve_downloads(info, platform_installers + info.extras)

    def get_available_downloads(
        self, store_app_id: str, details_cache=None,
    ) -> GameDownloadInfo:
        if self._downloads.get("backend") == "html":
            return self._html_downloads(store_app_id)
        return self._api_downloads(store_app_id)

    def _new_info(self, store_app_id: str) -> GameDownloadInfo:
        game = self._store._db.get_game(store_app_id)
        title = (game or {}).get("title") or store_app_id
        return GameDownloadInfo(
            game_title=title,
            store_name=self.store_name,
            store_app_id=store_app_id,
        )

    def _api_downloads(self, store_app_id: str) -> GameDownloadInfo:
        downloads = self._downloads
        game = self._store._db.get_game(store_app_id)

        # Some stores key their files endpoint by a plugin-DB field
        # rather than the app id (JAST: {translation_id}) — render with
        # the app id plus whatever the sync captured for the game.
        render = {"id": store_app_id}
        if game:
            render.update({k: v for k, v in game.items()
                           if isinstance(v, (str, int))})
        try:
            url = downloads["files_url_template"].format(**render)
        except (KeyError, IndexError) as e:
            raise ValueError(
                f"Cannot build the download listing URL for "
                f"'{store_app_id}' on {self.display_name}: missing "
                f"field {e} (sync the store first)"
            ) from e
        resp = self._store.http.get(
            url,
            headers=self._store._get_auth_headers(),
            cookies=self._store._get_auth_cookies(),
            timeout=30,
        )
        if resp.status_code in (401, 403):
            raise PermissionError(self.auth_failure_message)
        if resp.status_code == 404:
            raise ValueError(
                f"No downloads found for '{store_app_id}' "
                f"on {self.display_name}"
            )

        # Lazy import keeps core import time free of plugin machinery;
        # bundled plugins are the only plugin code (COMPILED_BUILD).
        from luducat.plugins.store_engine.backends import api as api_backend

        info = self._new_info(store_app_id)
        buckets = {
            "installers": info.installers,
            "patches": info.patches,
            "extras": info.extras,
        }
        fields = downloads.get("fields", {})
        data = resp.json()

        bucket_map = downloads.get("buckets")
        if bucket_map:
            # Route categories from the map; unknown ones log a warning.
            # Platform stamped from the map when configured (ZOOM),
            # otherwise the entry's own extracted field is kept (JAST).
            if isinstance(data, dict):
                for key in data:
                    if data[key] and key not in bucket_map:
                        logger.warning(
                            "[%s] unmapped download category %r "
                            "(%d files skipped)",
                            self.store_name, key, len(data[key]))
            for key, cfg in bucket_map.items():
                for item in api_backend.extract_items(data, key, fields):
                    if "platform" in cfg:
                        item["platform"] = cfg["platform"]
                    else:
                        item.setdefault("platform", "")
                    item["downlink"] = self._render_downlink(item)
                    buckets.get(cfg.get("bucket", "installers"),
                                info.installers).append(item)
            return info

        items = api_backend.extract_items(
            data,
            downloads.get("items_path", ""),
            fields,
        )
        type_map = downloads.get("type_map", {})
        default_bucket = type_map.get("", "installers")
        for item in items:
            item["downlink"] = self._render_downlink(item)
            bucket_name = type_map.get(item.get("type", ""), default_bucket)
            buckets.get(bucket_name, info.installers).append(item)
        return info

    def _html_downloads(self, store_app_id: str) -> GameDownloadInfo:
        """Scrape a whole-library HTML page for one game's downloads.

        MangaGamer has no per-game files endpoint: the paginated member
        page lists every owned game's download buttons, each carrying an
        S3_download.php path in its onclick. Scan pages, keep the buttons
        whose id_field matches this game, dedupe (a game can recur across
        order blocks), and stop when a page yields no buttons at all
        (past the last page) or max_pages is hit.
        """
        from luducat.plugins.store_engine.backends import html as html_backend

        downloads = self._downloads
        item_selector = downloads.get("item_selector", "")
        fields = downloads.get("fields", {})
        id_field = downloads.get("id_field")
        base = downloads.get("download_url_base", "")
        pagination = downloads.get("pagination", {})
        start = pagination.get("start", 1)
        max_pages = pagination.get("max_pages", 25)

        info = self._new_info(store_app_id)
        bucket = {
            "installers": info.installers,
            "patches": info.patches,
            "extras": info.extras,
        }.get(downloads.get("default_bucket", "installers"), info.installers)

        seen: set[str] = set()
        for page in range(start, start + max_pages):
            url = downloads["files_url_template"].format(
                id=store_app_id, page=page)
            resp = self._store.http.get(
                url,
                headers=self._store._get_auth_headers(),
                cookies=self._store._get_auth_cookies(),
                timeout=30,
            )
            if resp.status_code in (401, 403):
                raise PermissionError(self.auth_failure_message)
            items = html_backend.extract_items(
                resp.text, item_selector, fields)
            if not items:
                break  # past the last library page
            for item in items:
                if id_field and str(item.get(id_field, "")) != str(store_app_id):
                    continue
                downlink = (item.get("downlink", "") or "").replace(
                    "&amp;", "&")
                if base and downlink and not downlink.startswith(
                        ("http://", "https://")):
                    downlink = base + downlink
                if not downlink or downlink in seen:
                    continue
                seen.add(downlink)
                item["downlink"] = downlink
                item.setdefault("platform", "")
                bucket.append(item)
        return info

    @staticmethod
    def _parse_size(size) -> int | None:
        """Normalize a store size field to bytes.

        Stores report either a human string ("4.80 MB", ZOOM) or exact
        bytes as a string ("1553551924", JAST); integers pass through.
        """
        if isinstance(size, int):
            return size
        if not isinstance(size, str):
            return None
        s = size.strip()
        if s.isdigit():
            return int(s)
        return _parse_size_string(s)

    def _render_downlink(self, item: dict) -> str:
        """Render the stable per-file reference from the item's fields.

        Templates may use any extracted field ({file_id}, {game_id},
        ...). A missing field is a ruleset/response mismatch — fail
        loudly instead of writing broken references into the manifest.
        """
        template = self._downloads.get("download_url_template", "")
        try:
            return template.format(**item)
        except (KeyError, IndexError) as e:
            raise ValueError(
                f"download_url_template for {self.store_name} references "
                f"field {e} missing from the files response"
            ) from e

    def resolve_downloads(
        self,
        info: GameDownloadInfo,
        selections: list[dict],
    ) -> DownloadTarget:
        type_by_downlink = {}
        for bucket, archive_type in (
            (info.installers, ArchiveType.GAME_INSTALLER),
            (info.patches, ArchiveType.GAME_PATCH),
            (info.extras, ArchiveType.GAME_EXTRA),
        ):
            for item in bucket:
                type_by_downlink[item.get("downlink", "")] = archive_type

        # Hop-based stores (envelope/request/redirect) go out lazy:
        # worker-start resolution calls refresh_download_url for the
        # real CDN URL. Signed URLs carry auth in query params, so
        # store credentials must not reach the CDN host.
        link_request = self._downloads.get("link_request")
        lazy = bool(
            self._downloads.get("link_envelope")
            or link_request
            or self._downloads.get("link_redirect") is not None
        )
        cookies = None if lazy else self._store._get_auth_cookies()
        headers = None if lazy else self._store._get_auth_headers()

        files = []
        for sel in selections:
            downlink = sel.get("downlink", "")
            archive_type = type_by_downlink.get(downlink)
            if archive_type is None:
                raise ValueError(
                    f"Selection '{sel.get('name', downlink)}' is not among "
                    f"the offered downloads for {info.game_title}"
                )
            size = self._parse_size(sel.get("size"))
            metadata = {
                "downlink": downlink,
                "file_id": sel.get("file_id", ""),
                "language": sel.get("language", ""),
                "store": info.store_name,
            }
            # A POST hop re-issues the link generation on refresh; every
            # field its body references must survive in the manifest.
            if link_request:
                for meta_key in link_request.get("body_fields", {}).values():
                    if meta_key in sel:
                        metadata[meta_key] = sel[meta_key]
            if "part" in sel:
                metadata["part"] = sel["part"]
            files.append(ArchiveRequest(
                url="" if lazy else downlink,
                archive_type=archive_type,
                store_name=info.store_name,
                store_app_id=info.store_app_id,
                game_title=info.game_title,
                # The download engine adopts the real CDN filename at
                # resolve time (base.extract_filename_from_cdn_url).
                filename=sel.get("name", downlink),
                expected_size=size,
                cookies=cookies,
                headers=headers,
                version=sel.get("version"),
                metadata=metadata,
            ))

        return DownloadTarget(
            game_title=info.game_title,
            store_name=info.store_name,
            store_app_id=info.store_app_id,
            files=files,
        )

    def refresh_download_url(self, metadata: dict):
        """Re-resolve a stable file reference to a downloadable URL.

        Never raises — the manager marks the download FAILED with the
        auth-failure message when this returns None (base contract).
        Three shapes, decided by the ruleset:
          - link_request: POST a body built from metadata, extract url.
          - link_envelope: GET the endpoint, extract url from JSON.
          - plain: re-render the template, return it with fresh cookies.
        """
        metadata = metadata or {}

        link_request = self._downloads.get("link_request")
        if link_request:
            return self._refresh_via_post(metadata, link_request)

        if self._downloads.get("link_redirect") is not None:
            return self._refresh_via_redirect(metadata)

        file_id = metadata.get("file_id")
        if not file_id:
            return None
        url = self._downloads.get("download_url_template", "").format(
            file_id=file_id)
        if not url:
            return None

        envelope = self._downloads.get("link_envelope")
        if not envelope:
            return url, self._store._get_auth_cookies() or {}

        # Envelope hop: GET with store auth, extract signed URL from
        # the JSON body. JSON parse failure = Accept-header trap.
        data = self._fetch_json(
            self._store.http.get, url, envelope_kind="link envelope")
        if data is None:
            return None
        return self._extract_signed_url(data, envelope, url, "link envelope")

    def _refresh_via_post(self, metadata: dict, link_request: dict):
        """POST-based link generation (JAST generate-link shape).

        Rebuilds the request body from metadata so a 403-expiry refresh
        can re-issue it; a body field missing from metadata means the
        manifest row predates the field — fail closed, not with a
        malformed request.
        """
        body = {}
        for body_key, meta_key in link_request.get("body_fields", {}).items():
            value = metadata.get(meta_key)
            if value is None:
                logger.warning(
                    "[%s] link_request needs metadata field %r, absent",
                    self.store_name, meta_key)
                return None
            body[body_key] = value
        body.update(link_request.get("body_extra", {}))

        url = link_request.get("url", "")
        if not url:
            return None

        def _post(u, **kw):
            return self._store.http.post(u, json=body, **kw)

        data = self._fetch_json(_post, url, envelope_kind="link_request")
        if data is None:
            return None
        return self._extract_signed_url(data, link_request, url, "link_request")

    def _refresh_via_redirect(self, metadata: dict):
        """302-redirect resolution: GET with auth, extract Location."""
        downlink = metadata.get("downlink")
        if not downlink:
            return None
        try:
            resp = self._store.http.get(
                downlink,
                headers=self._store._get_auth_headers(),
                cookies=self._store._get_auth_cookies(),
                allow_redirects=False,
                timeout=30,
            )
        except Exception as e:
            logger.warning("[%s] link_redirect GET failed for %s: %s",
                           self.store_name, downlink, e)
            return None
        location = resp.headers.get("Location") or resp.headers.get("location")
        if not location:
            logger.warning(
                "[%s] link_redirect for %s did not redirect (HTTP %s)",
                self.store_name, downlink, resp.status_code)
            return None
        return location, {}

    def _fetch_json(self, method, url: str, envelope_kind: str):
        """GET/POST a hop endpoint with store auth, return parsed JSON.

        Returns None on non-200 or any transport/parse error (the
        Accept-trap HTML-with-200 lands here as a JSON parse failure).
        """
        try:
            resp = method(
                url,
                headers=self._store._get_auth_headers(),
                cookies=self._store._get_auth_cookies(),
                timeout=30,
            )
            if resp.status_code != 200:
                logger.warning("[%s] %s %s answered HTTP %s",
                               self.store_name, envelope_kind, url,
                               resp.status_code)
                return None
            return resp.json()
        except Exception as e:
            logger.warning("[%s] %s fetch failed for %s: %s",
                           self.store_name, envelope_kind, url, e)
            return None

    def _extract_signed_url(self, data, cfg: dict, url: str, kind: str):
        """Pull the signed CDN URL out of a hop response body."""
        url_field = cfg.get("url_field", "url")
        resolved = data.get(url_field) if isinstance(data, dict) else None
        if not resolved or not isinstance(resolved, str):
            logger.warning("[%s] %s response for %s has no usable %r field",
                           self.store_name, kind, url, url_field)
            return None
        # Signed URL: auth lives in the query string, no cookies (see
        # resolve_downloads on why credentials must not reach the CDN).
        return resolved, {}
