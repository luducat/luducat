# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# store.py

# Protocol documented from Nile (https://github.com/imLinguin/nile)
# Copyright (c) imLinguin and Nile contributors
# Licensed under GPLv3
# Clean-room implementation from analysis notes; endpoint constants and
# request field names are functional facts required by the Amazon API.
"""Amazon Games store plugin for luducat.

Library integration via Amazon's device-registration auth and the
Animus entitlements API. The entitlement response carries the full
metadata set (developer, publisher, genres, ratings, images) directly,
so there is no separate enrichment phase. Game launching is delegated
to runner plugins via the RuntimeManager.

Architecture:
    - Device-auth session management in amazon_session.py
    - Animus API client in amazon_api.py
    - catalog.db is a metadata cache; the owned set + syncPoint live in
      a sync-state row so incremental syncs still return complete
      ownership lists (core stale-game reconciliation depends on that)
"""

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from luducat.plugins.base import AbstractGameStore, Game, PluginError

from .amazon_api import AmazonApi
from .amazon_session import AmazonSession
from .database import AmazonDatabase, AmazonGame

logger = logging.getLogger(__name__)

# The syncPoint sent to Amazon is OUR previous sync timestamp; back it
# off by a margin so client/server clock skew cannot hide changes
# (overlap only re-upserts, which is harmless).
_SYNC_POINT_MARGIN = 300

# Incremental responses have not been observed to surface revocations,
# so force a full fetch this often to bound ownership drift.
_FULL_SYNC_INTERVAL = 7 * 86400

# ESRB rating string -> (IGDB-style code, minimum age)
_ESRB_MAP = {
    "Everyone": ("E", 0),
    "Everyone 10+": ("E10+", 10),
    "Teen": ("T", 13),
    "Mature": ("M", 17),
    "Adults Only": ("AO", 18),
}


def _age_ratings_from_details(
    details: Dict[str, Any],
) -> Tuple[List[Dict[str, str]], Optional[int]]:
    """Normalize Amazon's rating strings to IGDB-style age_ratings.

    Amazon sends prose strings ("Everyone", "PEGI 3", "USK 16",
    "Unrated"). The content filter matches on IGDB short codes
    (ESRB:M, PEGI:18, USK:18), so map accordingly and derive a numeric
    required_age from the strictest system.

    Returns:
        Tuple of (age_ratings list, required_age or None)
    """
    ratings: List[Dict[str, str]] = []
    ages: List[int] = []

    esrb = details.get("esrbRating")
    if esrb in _ESRB_MAP:
        code, age = _ESRB_MAP[esrb]
        ratings.append({"system": "ESRB", "rating": code})
        ages.append(age)

    for system, key in (("PEGI", "pegiRating"), ("USK", "uskRating")):
        value = details.get(key) or ""
        match = re.search(r"(\d+)", value)
        if match:
            age = int(match.group(1))
            ratings.append({"system": system, "rating": str(age)})
            ages.append(age)

    return ratings, (max(ages) if ages else None)


def _entitlement_to_db_game(
    entitlement: Dict[str, Any],
) -> Optional[AmazonGame]:
    """Convert one entitlement API record to the database model.

    Returns None for records without a product id (nothing to key on).
    """
    product = entitlement.get("product") or {}
    product_id = product.get("id")
    if not product_id:
        logger.warning("Entitlement without product id skipped")
        return None

    product_detail = product.get("productDetail") or {}
    details = product_detail.get("details") or {}

    age_ratings, required_age = _age_ratings_from_details(details)

    developers = []
    if details.get("developer"):
        developers.append(details["developer"])
    developers.extend(details.get("otherDevelopers") or [])

    websites = details.get("websites") or {}

    game = AmazonGame(
        product_id=product_id,
        entitlement_id=entitlement.get("id"),
        sku=product.get("sku"),
        state=entitlement.get("state"),
        title=product.get("title") or product_id,
        description=product.get("description"),
        short_description=details.get("shortDescription"),
        release_date=details.get("releaseDate"),
        esrb_rating=details.get("esrbRating"),
        pegi_rating=details.get("pegiRating"),
        usk_rating=details.get("uskRating"),
        required_age=required_age,
        icon_url=product_detail.get("iconUrl"),
        logo_url=details.get("logoUrl"),
        background_url=details.get("backgroundUrl1"),
        background_url2=details.get("backgroundUrl2"),
        crown_url=details.get("pgCrownImageUrl"),
        official_website=websites.get("OFFICIAL"),
    )
    game.developers = developers
    game.publishers = (
        [details["publisher"]] if details.get("publisher") else []
    )
    game.genres = details.get("genres") or []
    game.game_modes = details.get("gameModes") or []
    game.screenshots = details.get("screenshots") or []
    return game


class AmazonStore(AbstractGameStore):
    """Amazon Games store integration via direct Amazon APIs."""

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)

        self._session: Optional[AmazonSession] = None
        self._api: Optional[AmazonApi] = None
        self._db: Optional[AmazonDatabase] = None
        self._title_index: Optional[Dict[str, str]] = None

        logger.debug("AmazonStore initialized: data_dir=%s", data_dir)

    # =========================================================================
    # REQUIRED PROPERTIES
    # =========================================================================

    @property
    def store_name(self) -> str:
        return "amazon"

    @property
    def display_name(self) -> str:
        return "Amazon Games"

    # =========================================================================
    # REQUIRED METHODS - Availability & Authentication
    # =========================================================================

    def is_available(self) -> bool:
        """Always available — no external binary dependency."""
        return True

    def is_authenticated(self) -> bool:
        return self._get_session().has_session

    async def authenticate(self) -> bool:
        """Legacy authenticate method — kept for interface compatibility."""
        return self.is_authenticated()

    def build_signin_url(self) -> str:
        """Sign-in URL for the config dialog's browser hand-off."""
        return self._get_session().build_signin_url()

    def authenticate_with_redirect_url(
        self, redirect_url: str
    ) -> tuple[bool, str]:
        """Complete auth from the redirect URL the user pasted back.

        Returns:
            Tuple of (success, message)
        """
        session = self._get_session()
        try:
            code = session.extract_authorization_code(redirect_url)
        except ValueError as e:
            return False, str(e)
        try:
            result = session.register_device(code)
            account = result.get("given_name") or _("unknown")
            return True, _("Connected as {account}").format(account=account)
        except Exception as e:
            logger.error("Amazon authentication failed: %s", e)
            return False, str(e)

    def logout(self) -> tuple[bool, str]:
        """Deregister the device and wipe credentials + sync state."""
        try:
            self._get_session().deregister()
            if self._db is not None or self.get_database_path().exists():
                self._get_db().clear_sync_state()
            return True, _("Logged out successfully")
        except Exception as e:
            logger.error("Amazon logout failed: %s", e)
            return False, str(e)

    def get_account_identifier(self) -> Optional[str]:
        """Stable account id (given_name is neither stable nor unique)."""
        return self._get_session().user_id

    def get_auth_status(self) -> tuple:
        session = self._get_session()
        if not session.has_session:
            return False, _("Not connected")
        account = session.given_name or session.user_id
        if not account:
            return False, _("Not connected")
        return True, _("Connected as {account}").format(account=account)

    def get_full_status(self, _force_refresh: bool = False) -> Dict[str, Any]:
        session = self._get_session()
        return {
            "account": session.given_name,
            "account_id": session.user_id,
            "authenticated": session.has_session,
        }

    # =========================================================================
    # REQUIRED METHODS - Game Data
    # =========================================================================

    async def fetch_user_games(
        self,
        status_callback: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> List[str]:
        """Fetch the owned-game list, incrementally when possible.

        The syncPoint is OUR previous sync timestamp — the server
        filters to entitlements changed since then. Adds (state LIVE)
        and removals (any other state) merge into the owned set from
        the sync state to produce a complete list. Because removals
        have not been observed in incremental responses, a full fetch
        is forced whenever the last one is older than a week.

        Returns:
            Complete list of owned product ids

        Raises:
            PluginError: If not authenticated or the fetch fails
        """
        if status_callback:
            status_callback(_("Fetching Amazon Games library..."))

        if not self.is_authenticated():
            error_msg = _(
                "Not authenticated with Amazon Games. "
                "Please authenticate first."
            )
            logger.warning(error_msg)
            if status_callback:
                status_callback(_("Error: {message}").format(message=error_msg))
            raise PluginError(error_msg)

        session = self._get_session()
        try:
            access_token = await asyncio.to_thread(session.ensure_valid)
        except Exception as e:
            error_msg = _(
                "Amazon Games session has expired. Please re-authenticate "
                "in the Amazon plugin settings (Settings > Plugins > Amazon)."
            )
            logger.error("%s (%s)", error_msg, e)
            if status_callback:
                status_callback(_("Error: {message}").format(message=error_msg))
            raise PluginError(error_msg) from e

        db = self._get_db()
        state = db.get_sync_state()
        user_id = session.user_id
        sync_start = time.time()

        # Full fetch on first sync, account change, or when the last
        # full fetch is stale. The account check is belt-and-suspenders
        # beside the orchestrator's account reconciliation.
        full_sync = (
            state["sync_point"] is None
            or state["user_id"] != user_id
            or state["last_full_sync"] is None
            or sync_start - state["last_full_sync"] > _FULL_SYNC_INTERVAL
        )
        sync_point = (
            None if full_sync
            else state["sync_point"] - _SYNC_POINT_MARGIN
        )

        if status_callback:
            status_callback(_("Contacting Amazon Games servers..."))

        api = self._get_api()
        try:
            entitlements, completed = await asyncio.to_thread(
                api.get_entitlements,
                access_token,
                session.device_serial,
                sync_point=sync_point,
                cancel_check=cancel_check,
            )
        except RuntimeError as e:
            error_msg = str(e)
            if "401" in error_msg:
                error_msg = _(
                    "Amazon Games session has expired. Please "
                    "re-authenticate in the Amazon plugin settings."
                )
            logger.error(error_msg)
            if status_callback:
                status_callback(_("Error: {message}").format(message=error_msg))
            raise PluginError(error_msg) from e

        if not completed or (cancel_check and cancel_check()):
            # Partial data must not overwrite ownership state
            return []

        # Dedup by product id — last record wins
        by_product: Dict[str, Dict[str, Any]] = {}
        for entitlement in entitlements:
            pid = (entitlement.get("product") or {}).get("id")
            if pid:
                by_product[pid] = entitlement

        owned = set() if full_sync else set(state["owned_ids"])
        new_count = 0
        for pid, entitlement in by_product.items():
            db_game = _entitlement_to_db_game(entitlement)
            if db_game is None:
                continue
            if not db.get_game(pid):
                new_count += 1
            db.upsert_game(db_game)

            # Anything not LIVE (revoked, expired) leaves the owned set;
            # missing state is treated as LIVE (observed records always
            # carry it, but do not drop games on a lenient response).
            if (entitlement.get("state") or "LIVE") == "LIVE":
                owned.add(pid)
            else:
                owned.discard(pid)
        db.commit()

        db.save_sync_state(
            sync_point=sync_start,
            owned_ids=sorted(owned),
            user_id=user_id,
            last_full_sync=sync_start if full_sync else None,
        )
        self._title_index = None

        if status_callback:
            status_callback(
                _("Found {total} Amazon games ({new} new)").format(
                    total=len(owned), new=new_count
                )
            )
        logger.info(
            "Fetched Amazon library: %d owned (%d changed, %d new, %s sync)",
            len(owned), len(by_product), new_count,
            "full" if full_sync else "incremental",
        )
        return sorted(owned)

    async def fetch_game_metadata(
        self,
        app_ids: List[str],
        download_images: bool = False,
    ) -> List[Game]:
        """Build Game objects from the catalog cache.

        Entitlements already delivered everything — no enrichment calls.
        """
        db = self._get_db()
        games = []
        for product_id in app_ids:
            db_game = db.get_game(product_id)
            if not db_game:
                logger.warning("Game not in catalog.db: %s", product_id)
                continue
            game = self._db_game_to_plugin_game(db_game)
            if game:
                games.append(game)
        return games

    def get_database_path(self) -> Path:
        return self.data_dir / "catalog.db"

    # =========================================================================
    # OPTIONAL METHODS - Metadata interface
    # =========================================================================

    def get_store_page_url(self, app_id: str) -> str:
        return "https://gaming.amazon.com/home"

    def get_game_metadata(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Get standardized metadata for a single game."""
        db = self._get_db()
        game = db.get_game(app_id)
        if not game:
            return None
        return self._metadata_dict(game.to_dict())

    def _metadata_dict(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        """Map a to_dict() record to the resolver's field vocabulary."""
        age_ratings, _required = _age_ratings_from_details({
            "esrbRating": meta.get("esrb_rating"),
            "pegiRating": meta.get("pegi_rating"),
            "uskRating": meta.get("usk_rating"),
        })

        links = []
        if meta.get("official_website"):
            links.append(
                {"type": "official", "url": meta["official_website"]}
            )

        return {
            "title": meta.get("title"),
            "short_description": meta.get("short_description"),
            "description": meta.get("description"),
            # Amazon has no portrait cover asset; the icon is the best
            # store-native art. IGDB/SteamGridDB carry proper covers
            # via cross-store resolution.
            "cover": meta.get("icon_url"),
            "header_url": meta.get("background_url"),
            "hero": meta.get("background_url"),
            "logo_url": meta.get("logo_url"),
            "icon_url": meta.get("icon_url"),
            "screenshots": meta.get("screenshots", []),
            "release_date": meta.get("release_date"),
            "developers": meta.get("developers", []),
            "publishers": meta.get("publishers", []),
            "genres": meta.get("genres", []),
            "features": meta.get("game_modes", []),
            "platforms": ["Windows"],
            "age_ratings": age_ratings,
            "required_age": meta.get("required_age"),
            "links": links,
            "type": "game",
        }

    # === UNIFORM METADATA INTERFACE ===

    def get_metadata_for_store_game(
        self,
        store_name: str,
        store_id: str,
        normalized_title: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Resolve a game in the Amazon catalog cache.

        Direct id lookup for our own store, title lookup for
        cross-store resolution.
        """
        app_id = None
        if store_name == self.store_name:
            app_id = store_id
        elif normalized_title:
            app_id = self._find_app_id_by_title(normalized_title)
        if not app_id:
            return None
        return self.get_game_metadata(app_id)

    def _find_app_id_by_title(self, normalized_title: str) -> Optional[str]:
        if self._title_index is None:
            self._build_title_index()
        return self._title_index.get(normalized_title)

    def _build_title_index(self) -> None:
        from luducat.plugins.sdk.text import normalize_title

        self._title_index = {}
        for game in self._get_db().get_all_games():
            nt = normalize_title(game.title)
            if nt:
                self._title_index[nt] = game.product_id
        logger.debug(
            "Built Amazon title index: %d entries", len(self._title_index)
        )

    def get_games_metadata_bulk(
        self, app_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        db = self._get_db()
        raw = db.get_games_metadata_bulk(app_ids)
        return {
            product_id: self._metadata_dict(meta)
            for product_id, meta in raw.items()
        }

    def get_game_description(self, app_id: str) -> str:
        game = self._get_db().get_game(app_id)
        return game.description if game and game.description else ""

    def get_screenshots_for_app(self, app_id: str) -> List[str]:
        game = self._get_db().get_game(app_id)
        return game.screenshots if game else []

    def get_all_screenshot_urls(self) -> Dict[str, List[str]]:
        result = {}
        try:
            for game in self._get_db().get_all_games():
                if game.screenshots:
                    result[game.product_id] = game.screenshots
        except Exception as e:
            logger.error("Failed to get all screenshot URLs: %s", e)
        return result

    # =========================================================================
    # LIFECYCLE HOOKS
    # =========================================================================

    def on_enable(self) -> None:
        logger.info("Amazon Games plugin enabled")
        self._get_db().initialize()

    def on_disable(self) -> None:
        logger.info("Amazon Games plugin disabled")
        self.close()

    def on_sync_complete(self, progress_callback=None) -> Dict[str, Any]:
        return {}

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None
        self._session = None
        self._api = None
        logger.debug("Amazon Games plugin closed")

    # =========================================================================
    # PRIVATE METHODS
    # =========================================================================

    def _get_db(self) -> AmazonDatabase:
        if self._db is None:
            self._db = AmazonDatabase(self.get_database_path())
            self._db.initialize()
        return self._db

    def _get_session(self) -> AmazonSession:
        if self._session is None:
            self._session = AmazonSession(
                get_credential=self.get_credential,
                set_credential=self.set_credential,
                delete_credential=self.delete_credential,
                http_client=self.http,
            )
        return self._session

    def _get_api(self) -> AmazonApi:
        if self._api is None:
            self._api = AmazonApi(http_client=self.http)
        return self._api

    def _db_game_to_plugin_game(
        self, db_game: AmazonGame
    ) -> Optional[Game]:
        try:
            if not db_game.product_id or not db_game.title:
                return None
            return Game(
                store_app_id=db_game.product_id,
                store_name=self.store_name,
                title=db_game.title,
                # Heroic's store code for Amazon is "nile"
                launch_url=f"heroic://launch/nile/{db_game.product_id}",
                short_description=db_game.short_description,
                description=db_game.description,
                cover_image_url=db_game.icon_url,
                header_image_url=db_game.background_url,
                background_image_url=db_game.background_url,
                screenshots=db_game.screenshots or [],
                release_date=db_game.release_date,
                developers=db_game.developers or [],
                publishers=db_game.publishers or [],
                genres=db_game.genres or [],
                extra_metadata={
                    "sku": db_game.sku,
                    "entitlement_id": db_game.entitlement_id,
                },
            )
        except Exception as e:
            logger.error("Failed to convert AmazonGame to Game: %s", e)
            return None
