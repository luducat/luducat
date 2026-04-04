# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# store.py

"""Declarative store engine — StoreEngine meta-plugin and VirtualStore.

StoreEngine is a multi_store plugin that reads JSON rulesets and creates
one VirtualStore per ruleset. Each VirtualStore implements AbstractGameStore
and delegates all fetch/auth logic to the engine's backends.
"""

import hashlib
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlparse, parse_qs

from luducat.plugins.base import AbstractGameStore, AuthenticationError, Game
from luducat.plugins.sdk.network import HTTPError
from luducat.plugins.sdk.cookies import get_browser_cookie_manager

from .engine import Ruleset, load_rulesets, extract_json_path, apply_field_spec, absolutize_html_urls
from .backends import html as html_backend
from .backends import api as api_backend

logger = logging.getLogger(__name__)

# Delay between paginated requests (seconds)
_PAGE_DELAY = 0.5

# Ruleset field names → resolver-standard field names.
# Rulesets use whatever names make sense for the scraping target;
# this map normalizes them to what MetadataResolver expects.
_FIELD_NORMALIZATION = {
    "cover_url": "cover",
    "cover_detail_url": "cover",       # detail version takes priority
    "game_modes": "game_modes_detail",
    "languages": "supported_languages",
    "esrb_rating": "age_rating_esrb",
    "operating_systems": "platforms",
}

# Fields that contain URLs and need absolutization when relative.
_URL_FIELDS = {"cover", "screenshots", "header_url", "hero", "background_url"}

# Fields that may contain HTML with embedded relative URLs (src, href).
_HTML_FIELDS = {"description", "short_description"}

# Matches src="/" or href="/" in HTML content (not protocol-relative //).
_RE_RELATIVE_HTML_URL = re.compile(r'((?:src|href)\s*=\s*["\'])(/(?!/))', re.IGNORECASE)


class EngineDatabase:
    """Simple SQLite catalog DB for a virtual store.

    Stores fetched game data in a flat table. One DB per virtual store.
    """

    def __init__(self, db_path: Path):
        self._path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def _ensure_db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS games (
                    app_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    metadata_json TEXT DEFAULT '{}',
                    fetched_at REAL DEFAULT 0
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            self._conn.commit()
        return self._conn

    def upsert_game(self, app_id: str, title: str, metadata: dict) -> None:
        from luducat.core.json_compat import json
        conn = self._ensure_db()
        conn.execute(
            """INSERT INTO games (app_id, title, metadata_json, fetched_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(app_id) DO UPDATE SET
                   title=excluded.title,
                   metadata_json=excluded.metadata_json,
                   fetched_at=excluded.fetched_at""",
            (app_id, title, json.dumps(metadata), time.time()),
        )
        conn.commit()

    def get_game(self, app_id: str) -> Optional[dict]:
        from luducat.core.json_compat import json
        conn = self._ensure_db()
        row = conn.execute(
            "SELECT app_id, title, metadata_json FROM games WHERE app_id=?",
            (app_id,),
        ).fetchone()
        if row is None:
            return None
        meta = json.loads(row[2]) if row[2] else {}
        meta["app_id"] = row[0]
        meta["title"] = row[1]
        return meta

    def get_all_app_ids(self) -> List[str]:
        conn = self._ensure_db()
        return [
            r[0] for r in conn.execute("SELECT app_id FROM games").fetchall()
        ]

    def get_all_games(self) -> List[dict]:
        from luducat.core.json_compat import json
        conn = self._ensure_db()
        rows = conn.execute(
            "SELECT app_id, title, metadata_json FROM games"
        ).fetchall()
        results = []
        for row in rows:
            meta = json.loads(row[2]) if row[2] else {}
            meta["app_id"] = row[0]
            meta["title"] = row[1]
            results.append(meta)
        return results

    def get_meta(self, key: str) -> Optional[str]:
        conn = self._ensure_db()
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        conn = self._ensure_db()
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()

    def clear_detail_fetched(self) -> int:
        """Nullify _detail_fetched in all games' metadata_json.

        Returns count of rows cleared.
        """
        from luducat.core.json_compat import json
        conn = self._ensure_db()
        rows = conn.execute(
            "SELECT app_id, metadata_json FROM games "
            "WHERE json_extract(metadata_json, '$._detail_fetched') = 1"
        ).fetchall()
        if not rows:
            return 0
        for app_id, meta_json in rows:
            meta = json.loads(meta_json) if meta_json else {}
            meta.pop("_detail_fetched", None)
            conn.execute(
                "UPDATE games SET metadata_json=? WHERE app_id=?",
                (json.dumps(meta), app_id),
            )
        conn.commit()
        return len(rows)

    def get_ids_needing_detail(self) -> List[str]:
        """Return app_ids where detail has not been fetched (or was cleared)."""
        conn = self._ensure_db()
        rows = conn.execute(
            "SELECT app_id FROM games "
            "WHERE json_extract(metadata_json, '$._detail_fetched') IS NULL "
            "   OR json_extract(metadata_json, '$._detail_fetched') != 1"
        ).fetchall()
        return [r[0] for r in rows]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


class VirtualStore(AbstractGameStore):
    """A store instance driven entirely by a JSON ruleset.

    Created by StoreEngine for each loaded ruleset. Implements the
    full AbstractGameStore contract by delegating to engine backends.
    """

    def __init__(
        self,
        ruleset: Ruleset,
        engine: "StoreEngine",
        config_dir: Path,
        cache_dir: Path,
        data_dir: Path,
    ):
        super().__init__(config_dir, cache_dir, data_dir)
        self._ruleset = ruleset
        self._engine = engine
        self._db = EngineDatabase(data_dir / "catalog.db")
        self._cookie_auth_cache: tuple = (0.0, None)  # (timestamp, result)

    @property
    def store_name(self) -> str:
        return self._ruleset.store_name

    @property
    def display_name(self) -> str:
        return self._ruleset.display_name

    def is_available(self) -> bool:
        return True

    async def authenticate(self) -> bool:
        """Authenticate based on ruleset auth config."""
        auth = self._ruleset.auth
        auth_type = auth.get("type", "none")

        logger.debug("[%s] authenticate: type=%s", self.store_name, auth_type)

        if auth_type == "none":
            return True

        if auth_type == "browser_cookies":
            ok = self._auth_browser_cookies(auth)
            logger.debug("[%s] browser cookie auth: %s", self.store_name,
                         "success" if ok else "failed")
            return ok

        if auth_type == "bearer_redirect":
            # Bearer token obtained via browser redirect
            token = self.get_credential("bearer_token")
            if token:
                logger.debug("[%s] bearer token present", self.store_name)
                return True
            # Need user to visit login URL and paste token
            login_url = auth.get("login_url", "")
            logger.info(
                "[%s] auth: visit %s and paste the token from the redirect URL",
                self.store_name, login_url,
            )
            return False

        if auth_type == "form_login":
            raw = self.get_credential("session_cookies")
            if raw:
                logger.debug("[%s] form_login session cookies present", self.store_name)
                return True
            return False

        if auth_type == "api_token":
            token = self.get_credential("api_token")
            logger.debug("[%s] api_token present: %s", self.store_name, bool(token))
            return bool(token)

        return False

    def is_authenticated(self) -> bool:
        auth = self._ruleset.auth
        auth_type = auth.get("type", "none")

        if auth_type == "none":
            return True

        if auth_type == "browser_cookies":
            return self._check_browser_cookies(auth)

        if auth_type == "bearer_redirect":
            return bool(self.get_credential("bearer_token"))

        if auth_type == "form_login":
            return bool(self.get_credential("session_cookies"))

        if auth_type == "api_token":
            return bool(self.get_credential("api_token"))

        return False

    def _auth_browser_cookies(self, auth: dict) -> bool:
        """Try to import cookies from user's browser."""
        if not self.has_local_data_consent():
            return False
        try:
            cm = get_browser_cookie_manager()
            domain = auth.get("domain", "")
            cookie_name = auth.get("cookie_name", "")
            cookies, _browser = cm.get_cookies_for_domain(domain, cookie_name)
            if cookies:
                return True
        except Exception as e:
            logger.debug("Cookie check failed for %s: %s", self.store_name, e)
        return False

    def _check_browser_cookies(self, auth: dict) -> bool:
        """Check if browser cookies are available (non-destructive).

        Caches result for 30s to avoid probing the browser DB on every call.
        """
        import time
        cached_at, cached_result = self._cookie_auth_cache
        now = time.monotonic()
        if cached_result is not None and (now - cached_at) < 30.0:
            return cached_result
        if not self.has_local_data_consent():
            return False
        try:
            cm = get_browser_cookie_manager()
            domain = auth.get("domain", "")
            cookie_name = auth.get("cookie_name", "")
            cookies, _ = cm.get_cookies_for_domain(domain, cookie_name)
            result = bool(cookies)
        except Exception:
            result = False
        self._cookie_auth_cache = (now, result)
        return result

    def _get_auth_headers(self) -> Dict[str, str]:
        """Build auth headers based on ruleset auth config."""
        auth = self._ruleset.auth
        auth_type = auth.get("type", "none")
        headers = {}

        if auth_type == "bearer_redirect":
            token = self.get_credential("bearer_token")
            if token:
                headers["Authorization"] = f"Bearer {token}"

        if auth_type == "api_token":
            token = self.get_credential("api_token")
            if token:
                header_name = auth.get("token_header", "Authorization")
                prefix = auth.get("token_prefix", "Bearer ")
                headers[header_name] = f"{prefix}{token}"

        # Custom headers from ruleset
        for k, v in auth.get("headers", {}).items():
            headers[k] = v

        return headers

    def _get_auth_cookies(self) -> Optional[dict]:
        """Get cookies for authenticated requests.

        For browser_cookies/api_token: reads from browser cookie jar.
        For bearer_redirect: loads session cookies saved during login.
        """
        auth = self._ruleset.auth
        auth_type = auth.get("type", "none")

        if auth_type in ("bearer_redirect", "form_login"):
            raw = self.get_credential("session_cookies")
            logger.info("[%s] session_cookies credential: %s (len=%d)",
                        self.store_name,
                        "present" if raw else "missing",
                        len(raw) if raw else 0)
            if raw:
                from luducat.core.json_compat import json
                try:
                    parsed = json.loads(raw)
                    logger.info("[%s] parsed session cookies: keys=%s",
                                self.store_name, list(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__)
                    return parsed
                except Exception as e:
                    logger.error("[%s] failed to parse session_cookies: %s", self.store_name, e)
            return None

        if auth_type not in ("browser_cookies", "api_token"):
            return None

        if not self.has_local_data_consent():
            return None

        try:
            cm = get_browser_cookie_manager()
            domain = auth.get("domain", "")
            cookie_name = auth.get("cookie_name", "")
            cookies, _browser = cm.get_cookies_for_domain(domain, cookie_name)
            if cookies:
                return cookies
        except Exception as e:
            logger.debug("Cookie retrieval failed for %s: %s", self.store_name, e)
        return None

    async def fetch_user_games(
        self,
        status_callback: Optional[Callable] = None,
        cancel_check: Optional[Callable] = None,
    ) -> List[str]:
        """Fetch owned game IDs from the store."""
        library = self._ruleset.library
        backend_type = library.get("backend", "api")

        logger.debug("[%s] fetching library via %s backend", self.store_name, backend_type)
        all_items = []

        if backend_type == "html":
            all_items = await self._fetch_library_html(library, status_callback, cancel_check)
        elif backend_type == "api":
            all_items = await self._fetch_library_api(library, status_callback, cancel_check)

        logger.debug("[%s] library returned %d raw items", self.store_name, len(all_items))

        # Build app_ids list and check for changes before upserting
        app_ids = []
        skipped = 0
        upsert_items = []
        for item in all_items:
            app_id = str(item.get("id", ""))
            title = item.get("title", "Unknown")
            if not app_id:
                skipped += 1
                continue
            app_ids.append(app_id)
            upsert_items.append((app_id, title, item))

        # Hash the fetched data to detect changes since last sync
        from luducat.core.json_compat import json
        content_hash = hashlib.sha256(
            json.dumps(
                [(aid, t) for aid, t, _ in upsert_items],
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16]

        prev_hash = self._db.get_meta("library_hash")
        if prev_hash == content_hash and app_ids:
            logger.info("[%s] library unchanged (%d games, hash %s), skipping upsert",
                        self.store_name, len(app_ids), content_hash)
        else:
            for app_id, title, item in upsert_items:
                self._db.upsert_game(app_id, title, item)
            self._db.set_meta("library_hash", content_hash)
            logger.info("[%s] %d games stored in plugin DB (hash %s)",
                        self.store_name, len(app_ids), content_hash)

        if skipped:
            logger.debug("[%s] skipped %d items with no app_id", self.store_name, skipped)

        if status_callback:
            status_callback(f"Found {len(app_ids)} games")

        return app_ids

    async def _fetch_library_html(
        self,
        library: dict,
        status_callback: Optional[Callable],
        cancel_check: Optional[Callable],
    ) -> List[dict]:
        """Fetch library pages using HTML backend."""
        pagination = library.get("pagination", {})
        pag_type = pagination.get("type", "page_param")
        item_selector = library.get("item_selector", "")
        fields = library.get("fields", {})
        all_items = []

        # Get auth cookies
        cookies = self._get_auth_cookies()

        page = pagination.get("start", 1)
        while True:
            if cancel_check and cancel_check():
                break

            url = library["url"].replace("{page}", str(page))
            if status_callback:
                status_callback(f"Fetching page {page}...")

            try:
                kwargs = {"headers": self._get_auth_headers()}
                if cookies:
                    kwargs["cookies"] = cookies
                logger.debug("[%s] GET %s", self.store_name, url)
                resp = self.http.get(url, **kwargs)
                resp.raise_for_status()
                html_text = resp.text
                logger.debug("[%s] page %d: %d bytes", self.store_name, page, len(html_text))
            except HTTPError as e:
                if e.response is not None and e.response.status_code == 401:
                    raise AuthenticationError(
                        f"{self.store_name}: authentication failed (session expired)"
                    ) from e
                logger.error("Failed to fetch %s page %d: %s", self.store_name, page, e)
                break
            except Exception as e:
                logger.error("Failed to fetch %s page %d: %s", self.store_name, page, e)
                break

            # Detect session expiry (redirected to login page)
            fail_indicator = self._ruleset.auth.get("fail_indicator", "")
            if page == 1 and fail_indicator and fail_indicator in resp.url:
                logger.warning("[%s] session expired (redirected to %s), clearing stale cookies",
                               self.store_name, resp.url)
                self.delete_credential("session_cookies")
                if status_callback:
                    status_callback("Session expired — please re-connect")
                break

            items = html_backend.extract_items(html_text, item_selector, fields)
            logger.debug("[%s] page %d: %d items extracted", self.store_name, page, len(items))
            if not items:
                break

            all_items.extend(items)

            # Check pagination
            if pag_type == "next_link":
                next_url = html_backend.get_next_page_url(html_text, pagination)
                if not next_url:
                    break
                # For next_link, replace URL entirely
                library_url_parsed = urlparse(library["url"])
                if not next_url.startswith("http"):
                    next_url = f"{library_url_parsed.scheme}://{library_url_parsed.netloc}{next_url}"
                library = {**library, "url": next_url}
            else:
                # Pass item_selector to has_next_page for no_items check
                pag_with_selector = {**pagination, "item_selector": item_selector}
                if not html_backend.has_next_page(html_text, pag_with_selector, page):
                    break

            page += 1
            time.sleep(_PAGE_DELAY)

        return all_items

    async def _fetch_library_api(
        self,
        library: dict,
        status_callback: Optional[Callable],
        cancel_check: Optional[Callable],
    ) -> List[dict]:
        """Fetch library using API backend."""
        from luducat.core.json_compat import json

        pagination = library.get("pagination", {})
        items_path = library.get("items_path", "")
        fields = library.get("fields", {})
        all_items = []

        headers = {"Accept": "application/json"}
        headers.update(self._get_auth_headers())

        # Cookie-based auth (JAST pattern: cookie JWT → Bearer header)
        # Also send session cookies for authenticated requests
        auth = self._ruleset.auth
        cookies = None
        if auth.get("type") in ("browser_cookies", "bearer_redirect", "form_login"):
            cookies = self._get_auth_cookies()
            logger.info("[%s] auth cookies loaded: %s (keys: %s)",
                        self.store_name,
                        "yes" if cookies else "no",
                        list(cookies.keys()) if cookies else [])
            # JAST pattern: extract Bearer token from cookie value
            token_cookie = auth.get("token_cookie")
            if token_cookie and cookies:
                from urllib.parse import unquote
                raw = cookies.get(token_cookie, "")
                if raw:
                    # URL-decode and use as Authorization header
                    decoded = unquote(str(raw))
                    if decoded.startswith("Bearer "):
                        headers["Authorization"] = decoded
                    elif decoded:
                        headers["Authorization"] = f"Bearer {decoded}"

        logger.info("[%s] request headers: %s", self.store_name,
                    {k: (v[:20] + "..." if len(str(v)) > 20 else v) for k, v in headers.items()})

        page = pagination.get("start", 1)
        while True:
            if cancel_check and cancel_check():
                break

            url = library["url"].replace("{page}", str(page))
            if status_callback:
                status_callback(f"Fetching page {page}...")

            try:
                kwargs = {"headers": headers}
                if cookies:
                    kwargs["cookies"] = cookies
                logger.info("[%s] GET %s (cookies=%s)", self.store_name, url, bool(cookies))
                resp = self.http.get(url, **kwargs)
                resp.raise_for_status()
                data = resp.json()
            except HTTPError as e:
                if e.response is not None and e.response.status_code == 401:
                    raise AuthenticationError(
                        f"{self.store_name}: authentication failed (token expired or invalid)"
                    ) from e
                logger.error("Failed to fetch %s page %d: %s", self.store_name, page, e)
                break
            except Exception as e:
                logger.error("Failed to fetch %s page %d: %s", self.store_name, page, e)
                break

            items = api_backend.extract_items(data, items_path, fields)
            logger.debug("[%s] page %d: %d items extracted", self.store_name, page, len(items))
            if not items:
                break

            all_items.extend(items)

            # Pagination check
            has_more = api_backend.has_more_items(data, items_path, pagination, page)
            logger.debug("[%s] page %d: has_more=%s", self.store_name, page, has_more)
            if not has_more:
                break

            page += 1
            time.sleep(_PAGE_DELAY)

        return all_items

    def _detail_fingerprint(self) -> str:
        """SHA-256 fingerprint of the ruleset's detail section (16 hex chars).

        When the detail fields change (selector fixes, new fields), the
        fingerprint changes and stale _detail_fetched flags get cleared.
        """
        detail = self._ruleset.detail
        if not detail:
            return ""
        from luducat.core.json_compat import json
        blob = json.dumps(detail, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def _check_detail_fingerprint(self) -> None:
        """Clear _detail_fetched flags if the detail ruleset changed."""
        fp = self._detail_fingerprint()
        if not fp:
            return
        stored = self._db.get_meta("detail_fingerprint")
        if stored == fp:
            return
        cleared = self._db.clear_detail_fetched()
        self._db.set_meta("detail_fingerprint", fp)
        if stored is not None:
            logger.info("[%s] detail ruleset changed, cleared %d stale entries",
                        self.store_name, cleared)

    async def fetch_game_metadata(
        self,
        app_ids: List[str],
        download_images: bool = True,
        status_callback: Optional[Callable] = None,
        cancel_check: Optional[Callable] = None,
    ) -> List[Game]:
        """Fetch detailed metadata for given app IDs."""
        detail = self._ruleset.detail
        results = []
        detail_fetched = 0
        cache_hits = 0

        # Invalidate stale detail cache when ruleset fields change
        self._check_detail_fingerprint()

        logger.debug("[%s] fetching metadata for %d games (detail=%s)",
                     self.store_name, len(app_ids), bool(detail))

        for i, app_id in enumerate(app_ids):
            if cancel_check and cancel_check():
                break

            # Check DB first
            cached = self._db.get_game(app_id)
            if not cached:
                continue

            # Fetch detail if configured and not yet enriched
            if detail and not cached.get("_detail_fetched"):
                detail_data = await self._fetch_detail(app_id, cached, detail)
                if detail_data:
                    cached.update(detail_data)
                    cached["_detail_fetched"] = True
                    self._db.upsert_game(app_id, cached.get("title", ""), cached)
                    detail_fetched += 1
                    logger.debug("[%s] detail fetched for %s: %d fields",
                                 self.store_name, app_id, len(detail_data))
            else:
                cache_hits += 1

            # Convert to Game dataclass
            game = self._to_plugin_game(app_id, cached)
            if game:
                results.append(game)

            if status_callback and (i + 1) % 10 == 0:
                status_callback(f"Metadata {i + 1}/{len(app_ids)}")

        logger.info("[%s] metadata done: %d games, %d detail fetches, %d cache hits",
                    self.store_name, len(results), detail_fetched, cache_hits)
        return results

    async def _fetch_detail(
        self,
        app_id: str,
        game_data: dict,
        detail: dict,
    ) -> Optional[dict]:
        """Fetch detail page/API for a single game."""
        from luducat.core.json_compat import json

        url_template = detail.get("url_template", "")
        if not url_template:
            return None

        fallback_template = detail.get("fallback_url_template", "")

        # Replace placeholders
        url = url_template.replace("{id}", str(app_id))
        for key, val in game_data.items():
            if isinstance(val, str):
                url = url.replace("{" + key + "}", val)

        backend_type = detail.get("backend", "api")
        fields = detail.get("fields", {})

        # Build request kwargs (shared between primary and fallback)
        headers = {"Accept": "application/json"} if backend_type == "api" else {}
        headers.update(self._get_auth_headers())
        kwargs = {"headers": headers}

        auth = self._ruleset.auth
        if auth.get("type") in ("browser_cookies", "bearer_redirect", "form_login"):
            cookies = self._get_auth_cookies()
            if cookies:
                kwargs["cookies"] = cookies
                token_cookie = auth.get("token_cookie")
                if token_cookie:
                    from urllib.parse import unquote
                    raw = cookies.get(token_cookie, "")
                    if raw:
                        decoded = unquote(str(raw))
                        if decoded.startswith("Bearer "):
                            kwargs["headers"]["Authorization"] = decoded
                        elif decoded:
                            kwargs["headers"]["Authorization"] = f"Bearer {decoded}"

        # Try primary URL, then fallback if redirected away
        urls_to_try = [url]
        if fallback_template:
            fallback_url = fallback_template.replace("{id}", str(app_id))
            for key, val in game_data.items():
                if isinstance(val, str):
                    fallback_url = fallback_url.replace("{" + key + "}", val)
            urls_to_try.append(fallback_url)

        for try_url in urls_to_try:
            logger.debug("[%s] detail GET %s (backend=%s)", self.store_name, try_url, backend_type)
            try:
                resp = self.http.get(try_url, **kwargs)
                resp.raise_for_status()

                # Detect redirect away from detail page (e.g. r18 → index)
                if resp.url != try_url:
                    if try_url != urls_to_try[-1]:
                        logger.debug("[%s] detail redirected for %s, trying fallback",
                                     self.store_name, app_id)
                        continue
                    else:
                        logger.debug("[%s] detail redirected for %s (no more fallbacks)",
                                     self.store_name, app_id)
                        return None

                if backend_type == "api":
                    data = resp.json()
                    return api_backend.extract_detail(data, fields)

                # HTML detail extraction
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "html.parser")
                result = {}
                for field_name, spec in fields.items():
                    value = html_backend._extract_field(soup, spec)
                    value = apply_field_spec(value, spec)
                    if value is not None:
                        # Absolutize relative URLs in HTML content
                        if spec.get("html") and isinstance(value, str):
                            value = absolutize_html_urls(value, self._ruleset.homepage)
                        result[field_name] = value
                return result

            except Exception as e:
                logger.debug("Detail fetch failed for %s/%s: %s", self.store_name, app_id, e)

        return None

    def _to_plugin_game(self, app_id: str, data: dict) -> Optional[Game]:
        """Convert internal game dict to plugins.base.Game dataclass."""
        title = data.get("title", "")
        if not title:
            return None

        # Build launch URL
        launch = self._ruleset.launch
        launch_url = ""
        if launch:
            template = launch.get("url_template", "")
            if template:
                launch_url = template.replace("{id}", str(app_id))
                for key, val in data.items():
                    if isinstance(val, str):
                        launch_url = launch_url.replace("{" + key + "}", val)

        # Map extracted fields to Game dataclass
        screenshots = data.get("screenshots", [])
        if isinstance(screenshots, str):
            screenshots = [screenshots]

        developers = data.get("developers", [])
        if isinstance(developers, str):
            developers = [developers]

        publishers = data.get("publishers", [])
        if isinstance(publishers, str):
            publishers = [publishers]

        genres = data.get("genres", [])
        if isinstance(genres, str):
            genres = [genres]

        # Prefer detail-fetched cover over library listing cover
        cover = (
            data.get("cover_detail_url")
            or data.get("cover_url")
            or data.get("cover")
        )

        # Build extra_metadata from store-specific fields
        extra = {}
        if data.get("game_modes"):
            extra["game_modes"] = data["game_modes"]
        if data.get("languages"):
            extra["supported_languages"] = data["languages"]
        if data.get("esrb_rating"):
            extra["age_rating_esrb"] = data["esrb_rating"]
        if data.get("operating_systems"):
            extra["platforms"] = data["operating_systems"]
        if data.get("is_free") is not None:
            extra["is_free"] = data["is_free"]
        if data.get("price") is not None:
            extra["price"] = data["price"]

        logger.debug("[%s] game %s: '%s' cover=%s screenshots=%d extra=%d",
                     self.store_name, app_id, title,
                     "yes" if cover else "no", len(screenshots), len(extra))

        return Game(
            store_app_id=str(app_id),
            store_name=self.store_name,
            title=title,
            launch_url=launch_url,
            short_description=data.get("short_description"),
            description=data.get("description"),
            cover_image_url=cover,
            header_image_url=data.get("header_url"),
            background_image_url=data.get("background_url"),
            screenshots=screenshots,
            release_date=data.get("release_date"),
            developers=developers,
            publishers=publishers,
            genres=genres,
            extra_metadata=extra if extra else {},
        )

    def _normalize_metadata(self, data: dict) -> dict:
        """Map ruleset field names to resolver-standard names and fix URLs.

        Rulesets use field names that match their scraping targets (e.g.
        ``cover_url``, ``cover_detail_url``).  The resolver expects
        standardized names (``cover``, ``game_modes_detail``, …).
        Relative URLs are made absolute using the ruleset's homepage.
        """
        out: Dict[str, Any] = {}
        homepage = self._ruleset.homepage.rstrip("/")

        # Cover priority: detail > library > already-normalized
        cover = (
            data.get("cover_detail_url")
            or data.get("cover_url")
            or data.get("cover")
        )
        if cover:
            out["cover"] = self._absolutize_url(cover, homepage)

        for key, value in data.items():
            # Skip the raw cover variants — already handled above
            if key in ("cover_url", "cover_detail_url"):
                continue

            canonical = _FIELD_NORMALIZATION.get(key, key)

            # Don't overwrite cover that we already set with priority logic
            if canonical == "cover" and "cover" in out:
                continue

            # Absolutize URL fields
            if canonical in _URL_FIELDS:
                if isinstance(value, list):
                    value = [self._absolutize_url(u, homepage)
                             for u in value if isinstance(u, str)]
                elif isinstance(value, str):
                    value = self._absolutize_url(value, homepage)

            # Fix relative URLs embedded in HTML description content
            if canonical in _HTML_FIELDS and isinstance(value, str) and "/" in value:
                value = _RE_RELATIVE_HTML_URL.sub(
                    rf"\g<1>{homepage}/", value
                )

            out[canonical] = value

        # Inject store-level adult content baseline from ruleset
        adult_base = self._ruleset.content_filter.get("adult_base_confidence")
        if adult_base:
            out["store_adult_baseline"] = float(adult_base)

        return out

    @staticmethod
    def _absolutize_url(url: str, homepage: str) -> str:
        """Prepend homepage to relative URLs (starting with ``/``)."""
        if url and url.startswith("/"):
            return homepage + url
        return url

    def get_game_metadata(self, app_id: str) -> Optional[dict]:
        """Get cached metadata for a single game."""
        data = self._db.get_game(app_id)
        return self._normalize_metadata(data) if data else None

    def get_games_metadata_bulk(self, app_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Batch metadata from plugin DB (no HTTP)."""
        results: Dict[str, Dict[str, Any]] = {}
        for app_id in app_ids:
            cached = self._db.get_game(app_id)
            if cached:
                results[app_id] = self._normalize_metadata(cached)
        return results

    def get_ids_needing_refetch(self) -> List[str]:
        if not self._ruleset.detail:
            return []
        self._check_detail_fingerprint()
        return self._db.get_ids_needing_detail()

    def resolve_by_title(self, normalized_title: str) -> Optional[dict]:
        """Try to find a game in this store by normalized title.

        Used for cross-store enrichment: derives a slug from the title,
        checks local DB first, then tries the public API on cache miss.
        Caches 404s permanently to avoid repeat requests.

        Args:
            normalized_title: Normalized game title

        Returns:
            Metadata dict if found, None otherwise
        """
        cross_store = self._ruleset.raw.get("cross_store", {})
        if not cross_store:
            return None

        slug_pattern = cross_store.get("slug_from_title")
        if not slug_pattern:
            return None

        # Derive slug from title
        slug = self._slugify_title(normalized_title)
        if not slug:
            return None

        # Check local DB by slug
        cached = self._db.get_game(slug)
        if cached:
            if cached.get("_not_found"):
                return None  # Previously 404'd
            return cached

        # Try public API
        url_template = cross_store.get("url_template", "")
        if not url_template:
            return None

        url = url_template.replace("{slug}", slug)
        logger.debug("[%s] cross-store probe: %s", self.store_name, url)

        try:
            headers = {"Accept": "application/json"}
            # Add Referer for politeness
            referer = cross_store.get("referer_template", "")
            if referer:
                headers["Referer"] = referer.replace("{slug}", slug)

            resp = self.http.get(url, headers=headers)
            if resp.status_code == 404:
                # Cache the miss permanently
                self._db.upsert_game(slug, "", {"_not_found": True})
                logger.debug("[%s] cross-store miss (404): %s", self.store_name, slug)
                return None

            resp.raise_for_status()
            data = resp.json()

            # Extract fields from the response using detail field specs
            fields = cross_store.get("fields", {})
            if fields:
                from .backends import api as api_backend
                extracted = api_backend.extract_detail(data, fields)
                title = extracted.get("title") or data.get("product", {}).get("name", slug)
                extracted["title"] = title
                self._db.upsert_game(slug, title, extracted)
                logger.debug("[%s] cross-store hit: %s (%s)", self.store_name, slug, title)
                return self._db.get_game(slug)
            else:
                return None

        except Exception as e:
            logger.debug("[%s] cross-store probe failed for %s: %s",
                         self.store_name, slug, e)
            return None

    @staticmethod
    def _slugify_title(title: str) -> str:
        """Convert a title to a URL slug (ZOOM-compatible).

        "Tyrian 2000" → "tyrian-2000"
        "Shadow Warrior: Classic Redux" → "shadow-warrior-classic-redux"
        """
        slug = title.lower().strip()
        slug = re.sub(r"[:\-\u2013\u2014/\\]+", "-", slug)
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"[^a-z0-9\-]", "", slug)
        slug = re.sub(r"-{2,}", "-", slug)
        slug = slug.strip("-")
        return slug

    # ─── Login / verify / logout (bearer_redirect) ────────────────

    def login_with_credentials(
        self, email: str, password: str,
    ) -> Tuple[bool, str]:
        """Direct HTTP login for bearer_redirect and form_login stores."""
        auth = self._ruleset.auth
        auth_type = auth.get("type", "")

        if auth_type == "form_login":
            return self._login_form_post(email, password)

        # bearer_redirect flow
        login_url = auth.get("login_url", "")
        if not login_url:
            return False, "No login URL configured"

        parsed = urlparse(login_url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        try:
            # Step 1: GET login page -> CSRF token + session cookies
            resp = self.http.get(login_url, timeout=15)
            resp.raise_for_status()
            m = re.search(r'csrf-token"\s+content="([^"]+)"', resp.text)
            if not m:
                return False, "Could not find CSRF token on login page"
            csrf = m.group(1)
            cookies = resp.cookies

            # Step 2: POST login (carry session cookies from step 1)
            resp2 = self.http.post(
                f"{base}/login",
                data={"_token": csrf, "email": email, "password": password},
                headers={"Referer": login_url, "Origin": base},
                cookies=cookies,
                allow_redirects=True,
                timeout=15,
            )
            if "/login" in resp2.url:
                return False, "Login failed -- check email and password"

            # Merge cookies from both responses
            all_cookies = {**dict(cookies), **dict(resp2.cookies)}

            # Step 3: GET user profile -> extract token
            user_url = auth.get("user_url", f"{base}/public/user")
            resp3 = self.http.get(
                user_url,
                headers={"Accept": "application/json"},
                cookies=all_cookies,
                timeout=10,
            )
            resp3.raise_for_status()
            user_data = resp3.json()

            token_field = auth.get("token_field", "li_token")
            token = user_data.get(token_field)
            if not token:
                return False, "Login succeeded but no token returned"

            self.set_credential("bearer_token", str(token))
            # Persist session cookies for endpoints that need them (e.g. /public/)
            from luducat.core.json_compat import json
            self.set_credential("session_cookies", json.dumps(all_cookies))
            username = user_data.get("name", user_data.get("screen_name", ""))
            logger.info("[%s] logged in as %s", self.store_name, username)
            return True, username

        except Exception as e:
            logger.error("[%s] login failed: %s", self.store_name, e)
            return False, str(e)

    def _login_form_post(
        self, email: str, password: str,
    ) -> Tuple[bool, str]:
        """Form-based HTTP login (POST credentials, capture session cookies).

        1. GET homepage to establish session cookie
        2. POST login URL with configured form fields
        3. Store all cookies (session + extra) in keyring
        """
        auth = self._ruleset.auth
        homepage = self._ruleset.homepage
        login_url = auth.get("login_url", "")
        if not login_url:
            return False, "No login_url configured"

        try:
            # Use the plugin's raw session (cookie jar persists across requests)
            sess = self.http.session

            # Step 1: GET homepage to establish session cookie
            if homepage:
                sess.get(homepage, timeout=15)

            # Step 2: POST login with configured fields
            form_data = {}
            for field in auth.get("form_fields", []):
                name = field["name"]
                value = field.get("value", "")
                if value == "{email}":
                    value = email
                elif value == "{password}":
                    value = password
                form_data[name] = value

            resp = sess.post(login_url, data=form_data,
                             allow_redirects=True, timeout=15)

            # Check for login failure (still on login page)
            fail_indicator = auth.get("fail_indicator", "")
            if fail_indicator and fail_indicator in resp.url:
                return False, "Login failed -- check email and password"

            # Collect all cookies from the session jar
            all_cookies = dict(sess.cookies)

            # Add extra cookies defined in ruleset (e.g. age gate)
            for extra in auth.get("extra_cookies", []):
                all_cookies[extra["name"]] = extra["value"]

            if not all_cookies:
                return False, "Login returned no session cookies"

            # Persist cookies
            from luducat.core.json_compat import json
            self.set_credential("session_cookies", json.dumps(all_cookies))
            self.set_credential("login_username", email)
            logger.info("[%s] form login ok, cookies: %s",
                        self.store_name, list(all_cookies.keys()))
            return True, email

        except Exception as e:
            logger.error("[%s] form login failed: %s", self.store_name, e)
            return False, str(e)

    def verify_token(self) -> Tuple[bool, str]:
        """Check if stored bearer token is still valid.

        Offline-safe: if network is unreachable and a token exists,
        assume it's still valid rather than showing "not connected".
        """
        token = self.get_credential("bearer_token")
        if not token:
            return False, _("Not logged in")

        verify_url = self._ruleset.auth.get("verify_url", "")
        if not verify_url:
            return True, "Token present (no verify endpoint)"

        try:
            resp = self.http.get(
                verify_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json() if resp.text else {}
                name = data.get("name", data.get("screen_name", ""))
                return True, name or "Authenticated"
            elif resp.status_code == 401:
                return False, "Token expired or invalid"
            else:
                return False, f"Verify returned {resp.status_code}"
        except Exception as e:
            # Network error with token present — assume still valid (offline mode)
            logger.debug("[%s] verify failed (offline?): %s", self.store_name, e)
            return True, _("Token present (offline)")

    def get_auth_status(self) -> Tuple[bool, str]:
        """Return auth status with descriptive message for the config dialog."""
        auth_type = self._ruleset.auth.get("type", "none")
        if auth_type == "none":
            return True, _("No authentication required")
        if auth_type == "browser_cookies":
            if self.is_authenticated():
                return True, _("Browser cookies available")
            return False, _("Not connected")

        if auth_type == "form_login":
            if self.is_authenticated():
                username = self.get_credential("login_username")
                if username:
                    return True, _("Logged in as \"{}\"").format(username)
                return True, _("Connected")
            return False, _("Not connected")

        # bearer_redirect / api_token — verify token
        ok, msg = self.verify_token()
        if ok:
            if msg and msg != "Authenticated":
                return True, _("Logged in as \"{}\"").format(msg)
            return True, _("Connected")
        return False, msg or _("Not connected")

    def logout(self) -> None:
        """Clear stored bearer token and session cookies."""
        self.delete_credential("bearer_token")
        self.delete_credential("session_cookies")
        self.delete_credential("login_username")
        logger.info("[%s] logged out", self.store_name)

    def get_config_actions(self) -> list:
        """Return login/logout actions for bearer_redirect stores."""
        from luducat.plugins.base import ConfigAction

        auth_type = self._ruleset.auth.get("type", "none")

        if auth_type == "browser_cookies" and self._ruleset.auth.get("login_url"):
            return [
                ConfigAction(
                    id="login",
                    label=_("Login to {}...").format(self._ruleset.display_name),
                    callback=self._show_browser_login_dialog,
                    group="auth",
                ),
                ConfigAction(
                    id="logout",
                    label=_("Logout"),
                    callback=self._do_logout,
                    group="auth",
                ),
            ]

        if auth_type not in ("bearer_redirect", "form_login"):
            return []

        return [
            ConfigAction(
                id="login",
                label=_("Login to {}...").format(self._ruleset.display_name),
                callback=self._show_login_dialog,
                group="auth",
            ),
            ConfigAction(
                id="logout",
                label=_("Logout"),
                callback=self._do_logout,
                group="auth",
            ),
        ]

    def _show_login_dialog(self) -> None:
        """Show email/password dialog and perform login."""
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QFormLayout, QLineEdit,
            QDialogButtonBox, QMessageBox, QApplication,
        )
        from PySide6.QtCore import Qt

        parent = QApplication.activeWindow()
        dlg = QDialog(parent)
        dlg.setWindowTitle(_("Login to {}").format(self._ruleset.display_name))
        dlg.setMinimumWidth(380)

        layout = QVBoxLayout(dlg)
        form = QFormLayout()

        email_input = QLineEdit()
        email_input.setPlaceholderText("you@example.com")
        form.addRow(_("Email:"), email_input)

        pass_input = QLineEdit()
        pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(_("Password:"), pass_input)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        email = email_input.text().strip()
        password = pass_input.text()
        if not email or not password:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, msg = self.login_with_credentials(email, password)
        finally:
            QApplication.restoreOverrideCursor()

        if ok:
            QMessageBox.information(
                parent, self._ruleset.display_name,
                _("Logged in as \"{}\"").format(msg),
            )
        else:
            QMessageBox.warning(
                parent, self._ruleset.display_name,
                _("Login failed: {}").format(msg),
            )
        self._emit_connection_status(ok)

    def _do_logout(self) -> None:
        self.logout()
        self._cookie_auth_cache = (0.0, None)
        self._emit_connection_status(False)

    def _emit_connection_status(self, is_authenticated: bool) -> None:
        """Notify any open config/settings dialog about auth status change."""
        try:
            from PySide6.QtWidgets import QApplication, QDialog
            for widget in QApplication.topLevelWidgets():
                for target in [widget] + widget.findChildren(QDialog):
                    if hasattr(target, 'connection_status_changed'):
                        target.connection_status_changed.emit(
                            self.store_name, is_authenticated,
                        )
                    if hasattr(target, 'update_plugin_status'):
                        target.update_plugin_status(
                            self.store_name, is_authenticated,
                        )
        except Exception:
            pass

    def get_login_config(self):
        """Browser login config for stores using browser_cookies auth."""
        auth = self._ruleset.auth
        if auth.get("type") != "browser_cookies" or not auth.get("login_url"):
            return None
        try:
            from luducat.plugins.sdk.dialogs import get_browser_login_config_class
            BrowserLoginConfig = get_browser_login_config_class()
        except ImportError:
            return None
        if BrowserLoginConfig is None:
            return None
        return BrowserLoginConfig(
            name=self._ruleset.display_name,
            login_url=auth["login_url"],
            cookie_domain=auth.get("domain", ""),
            required_cookie=auth.get("cookie_name", ""),
        )

    def _show_browser_login_dialog(self) -> None:
        """Show browser login dialog for cookie-based auth."""
        login_config = self.get_login_config()
        if not login_config:
            return
        from PySide6.QtWidgets import QApplication
        from luducat.ui.dialogs.oauth_dialog import BrowserLoginDialog

        logger.debug("[%s] opening browser login dialog", self.store_name)
        parent = QApplication.activeWindow()
        dialog = BrowserLoginDialog(login_config, parent)
        dialog.exec()
        cookies = dialog.get_cookies()
        dialog.deleteLater()
        ok = bool(cookies)
        self._cookie_auth_cache = (0.0, None)  # invalidate
        logger.info("[%s] browser login %s", self.store_name, "succeeded" if ok else "cancelled/failed")
        self._emit_connection_status(ok)

    def get_database_path(self) -> Path:
        return self.data_dir / "catalog.db"

    def get_store_page_url(self, app_id: str, store_name: str = "") -> str:
        launch = self._ruleset.launch
        if launch:
            template = launch.get("url_template", "")
            if template:
                return template.replace("{id}", str(app_id))
        return self._ruleset.homepage

    def close(self):
        self._db.close()


class StoreEngine(AbstractGameStore):
    """Meta-plugin that spawns VirtualStore instances from JSON rulesets.

    This class itself is never used as a store — it creates VirtualStore
    instances via get_store_instances(). The plugin manager calls this
    method when plugin.json has "multi_store": true.
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._rulesets: List[Ruleset] = []
        self._virtual_stores: List[VirtualStore] = []

    @property
    def store_name(self) -> str:
        return "store_engine"

    @property
    def display_name(self) -> str:
        return "Declarative Store Engine"

    def is_available(self) -> bool:
        return True

    async def authenticate(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return True

    async def fetch_user_games(self, **kwargs) -> List[str]:
        return []

    async def fetch_game_metadata(self, app_ids, **kwargs) -> List[Game]:
        return []

    def get_database_path(self) -> Path:
        return self.data_dir / "engine.db"

    def get_store_instances(self) -> List[VirtualStore]:
        """Load rulesets and create one VirtualStore per ruleset."""
        if self._virtual_stores:
            return self._virtual_stores

        # Bundled rulesets
        bundled_dir = Path(__file__).parent / "rulesets"

        # User-contributed rulesets
        from luducat.core.config import get_config_dir
        user_dir = get_config_dir() / "plugins" / "store_engine" / "rulesets"

        self._rulesets = load_rulesets(bundled_dir, user_dir)
        logger.info("Loaded %d store rulesets", len(self._rulesets))

        for ruleset in self._rulesets:
            vs_data_dir = self.data_dir / ruleset.store_name
            vs_cache_dir = self.cache_dir / ruleset.store_name
            vs_config_dir = self.config_dir / ruleset.store_name

            vs = VirtualStore(
                ruleset=ruleset,
                engine=self,
                config_dir=vs_config_dir,
                cache_dir=vs_cache_dir,
                data_dir=vs_data_dir,
            )
            self._virtual_stores.append(vs)
            logger.debug("Created virtual store: %s (%s)", ruleset.store_name,
                         ruleset.display_name)

        return self._virtual_stores

    def close(self):
        for vs in self._virtual_stores:
            vs.close()
