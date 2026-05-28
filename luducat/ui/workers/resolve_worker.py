# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Background worker for resolving download URLs and product IDs.

Runs handler lookups, auth checks, and API calls off the main thread.
Emits Qt signals for success/error so the UI can react.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QThread, Signal

from luducat.core.download_handlers import find_handler, get_handler

logger = logging.getLogger(__name__)

try:
    _("")
except NameError:
    def _(s): return s


def _resolve_url_sync(url: str) -> dict:
    """Resolve a URL to a DownloadTarget (blocking).

    Returns dict with:
      - {"type": "target", "target": DownloadTarget}
      - {"type": "error", "message": str}
    """
    logger.debug("Resolving URL: %s", url)
    handler = find_handler(url)
    if handler is None:
        logger.debug("No handler found for URL: %s", url)
        return {
            "type": "error",
            "message": _("URL not supported. No download handler found."),
        }

    logger.debug("Handler: %s (%s)", handler.store_name, handler.display_name)
    auth = handler.check_auth()
    logger.debug("Auth check: %s", "ok" if auth else auth.message)
    if not auth:
        return {"type": "error", "message": auth.message}

    try:
        target = handler.resolve_url(url)
        logger.debug("Resolved to: %s (%d files)",
                      target.game_title, len(target.files))
        return {"type": "target", "target": target}
    except PermissionError as e:
        return {"type": "error", "message": str(e)}
    except ValueError as e:
        return {"type": "error", "message": str(e)}
    except Exception as e:
        logger.exception("Unexpected error resolving URL %s", url)
        return {"type": "error", "message": str(e)}


def _resolve_product_sync(store_name: str, store_app_id: str) -> dict:
    """Resolve a product ID to a GameDownloadInfo (blocking).

    Returns dict with:
      - {"type": "info", "info": GameDownloadInfo, "handler": handler}
      - {"type": "error", "message": str}
    """
    logger.debug("Resolving product: %s/%s", store_name, store_app_id)
    handler = get_handler(store_name)
    if handler is None:
        logger.debug("No handler for store: %s", store_name)
        return {
            "type": "error",
            "message": _("No download handler for store '{store}'.").format(
                store=store_name
            ),
        }

    auth = handler.check_auth()
    logger.debug("Auth check (%s): %s", store_name, "ok" if auth else auth.message)
    if not auth:
        return {"type": "error", "message": auth.message}

    try:
        info = handler.get_available_downloads(store_app_id)
        logger.debug("Available downloads for %s: %d installers, %d extras",
                      store_app_id, len(info.installers), len(info.extras))
        return {"type": "info", "info": info, "handler": handler}
    except PermissionError as e:
        return {"type": "error", "message": str(e)}
    except ValueError as e:
        return {"type": "error", "message": str(e)}
    except Exception as e:
        logger.exception(
            "Unexpected error resolving %s/%s", store_name, store_app_id,
        )
        return {"type": "error", "message": str(e)}


class ResolveWorker(QThread):
    """Background thread for download URL/product resolution.

    Usage:
        worker = ResolveWorker(url="https://gog.com/game/bg")
        worker.resolved.connect(on_resolved)
        worker.error.connect(on_error)
        worker.start()
    """

    resolved = Signal(dict)   # {"type": "target"|"info", ...}
    error = Signal(str)       # error message

    def __init__(
        self,
        url: Optional[str] = None,
        store_name: Optional[str] = None,
        store_app_id: Optional[str] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._store_name = store_name
        self._store_app_id = store_app_id

    def run(self) -> None:
        if self._url:
            result = _resolve_url_sync(self._url)
        elif self._store_name and self._store_app_id:
            result = _resolve_product_sync(self._store_name, self._store_app_id)
        else:
            self.error.emit(_("No URL or product ID provided."))
            return

        if result["type"] == "error":
            self.error.emit(result["message"])
        else:
            self.resolved.emit(result)
