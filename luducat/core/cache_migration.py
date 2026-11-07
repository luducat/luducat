# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat-project@trinity2k.net
#

"""One-time reunification of a parallel AppImage cache tree.

The 0.8.5 sharun AppImage ran with XDG_CACHE_HOME redirected to
~/.cache/AppImage-Cache (upstream AppRun behavior, lud-vuvy), so it
built a second cache universe next to the real one. This merges that
tree back: files are copied only where the real cache has none
(timestamps preserved), the Steam app-type DB is merged row-wise, and
the legacy tree is removed only after a fully successful merge.
"""

import contextlib
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from luducat.core.json_compat import json

logger = logging.getLogger(__name__)

_LEGACY_PARENT = "AppImage-Cache"


def migrate_appimage_cache(target: Path) -> bool:
    """Merge <cache-parent>/AppImage-Cache/<name>/ into the real cache.

    Returns True when a migration ran to completion. Any failure logs
    a warning and leaves the legacy tree in place for the next start.
    """
    if not isinstance(target, Path):
        raise TypeError(f"target must be a Path, got {type(target)!r}")

    legacy = target.parent / _LEGACY_PARENT / target.name
    if not legacy.is_dir():
        return False
    # A still-redirected old AppImage resolves target inside
    # AppImage-Cache; never merge a tree into itself.
    if legacy.resolve() == target.resolve():
        return False

    files_copied = 0
    rows_merged = 0
    try:
        for src in sorted(legacy.rglob("*")):
            if not src.is_file() or src.is_symlink():
                continue
            rel = src.relative_to(legacy)
            dest = target / rel
            if src.name == "app_types.db":
                rows_merged += _merge_app_type_db(src, dest)
            elif src.name == "gap_games.json":
                _merge_gap_cache(src, dest)
            elif not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                files_copied += 1
        shutil.rmtree(legacy)
    except (OSError, sqlite3.Error, ValueError) as e:
        logger.warning(
            "AppImage cache migration failed, keeping %s for next "
            "start: %s", legacy, e,
        )
        return False

    logger.info(
        "AppImage cache merged back into %s: %d files copied, "
        "%d app-type rows merged, legacy tree removed",
        target, files_copied, rows_merged,
    )
    return True


def _merge_app_type_db(src: Path, dest: Path) -> int:
    """Row-wise merge of a Steam app-type cache DB.

    Precedence per appid: a typed row beats an unknown (NULL) row;
    between two typed rows the newer resolved_at wins; between two
    unknown rows the host row stays. Pre-v2 DBs without resolved_at
    are treated as stamp 0 on either side.
    """
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        conn = sqlite3.connect(str(dest))
        try:
            count = conn.execute("SELECT COUNT(*) FROM apps").fetchone()[0]
        finally:
            conn.close()
        return count

    conn = sqlite3.connect(str(dest))
    try:
        if not _has_column(conn, "apps", "resolved_at"):
            # Column only; the plugin's own schema migration stamps
            # typed rows and bumps user_version when it next opens.
            conn.execute(
                "ALTER TABLE apps ADD COLUMN "
                "resolved_at INTEGER NOT NULL DEFAULT 0"
            )

        conn.execute("ATTACH DATABASE ? AS legacy", (str(src),))
        src_stamp = (
            "COALESCE(resolved_at, 0)"
            if _has_column(conn, "apps", "resolved_at", schema="legacy")
            else "0"
        )
        cursor = conn.execute(f"""
            INSERT INTO apps (appid, type, name, is_free, resolved_at)
            SELECT appid, type, name, is_free, {src_stamp}
            FROM legacy.apps WHERE true
            ON CONFLICT(appid) DO UPDATE SET
                type = excluded.type,
                name = excluded.name,
                is_free = excluded.is_free,
                resolved_at = excluded.resolved_at
            WHERE (excluded.type IS NOT NULL AND apps.type IS NULL)
               OR (excluded.type IS NOT NULL AND apps.type IS NOT NULL
                   AND excluded.resolved_at > apps.resolved_at)
        """)
        conn.commit()
        return cursor.rowcount if cursor.rowcount > 0 else 0
    finally:
        conn.close()


def _has_column(conn, table: str, column: str, schema: str = "main") -> bool:
    rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _merge_gap_cache(src: Path, dest: Path) -> None:
    """Union-merge gap_games.json lists, host entries first."""
    try:
        legacy_ids = json.loads(src.read_text(encoding="utf-8"))
        if not isinstance(legacy_ids, list):
            return
    except (OSError, json.JSONDecodeError):
        logger.debug("Skipping unreadable legacy gap cache: %s", src)
        return

    host_ids: list = []
    if dest.exists():
        try:
            host_ids = json.loads(dest.read_text(encoding="utf-8"))
            if not isinstance(host_ids, list):
                host_ids = []
        except (OSError, json.JSONDecodeError):
            host_ids = []

    seen = set(host_ids)
    merged = host_ids + [g for g in legacy_ids if g not in seen]
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(merged))
        os.replace(tmp_name, dest)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
