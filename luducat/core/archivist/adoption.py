# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Archive adoption importer (Phase E) -- seeds the manifest from disk.

Adopts a pre-existing installer archive in place: files are never moved,
renamed or re-stamped, only indexed. Checksum verification is deferred
(empty checksum + adopted flag mark rows for a later verification pass).

Slug attribution is directory-first: the volume layout names the game
directory after the GOG slug, and that is far more reliable than the
filename (real archives hold setup_p.a.m.e.l.a._* inside pamelar/, and
DLC installers inside the base game's folder). The filename supplies
version, build id, platform and kind; its slug is only a fallback when
the layout yields none (flat organization).

The plan sketched an "extra-guess" kind on ParsedInstaller; in practice
extras carry arbitrary names (Tomb_Raider_Comic.zip), so the scanner
classifies them by extension within an attributed game directory instead
of guessing from the name.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, TYPE_CHECKING

from luducat.core.archivist.audit import (
    _EXT_PLATFORM,
    _filename_build,
    _filename_stem,
    _filename_version,
)
from luducat.core.archivist.types import ArchiveEntry, ArchiveType
from luducat.core.archivist.volume import _sanitize_title, _slugify

if TYPE_CHECKING:
    from luducat.core.archivist.manager import ArchivistManager

logger = logging.getLogger(__name__)

# Sidecar files written by lgogdownloader or luducat itself next to the
# installers (cover art, icons, generated changelogs, parked previous
# builds, logs). They are not store downloads and never enter the
# manifest; the report lists them so nothing disappears silently.
IGNORED_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "bmp", "ico", "svg",
    "html", "htm", "txt", "log", "old", "lock", "tmp", "part", "ini",
})

# Store extras (soundtracks, manuals, artbooks, wallpapers) ship in
# these containers. Anything else non-installer is reported unmatched.
EXTRA_EXTENSIONS = frozenset({
    "zip", "rar", "7z", "gz", "tgz", "bz2", "xz", "pdf",
    "mp3", "mp4", "avi", "mkv", "flac", "ogg", "opus", "wav",
    "epub", "mobi", "cbr", "cbz",
})

# New-style GOG unix/mac installers end in the numeric build id instead
# of a parenthesized one: graveyard_keeper_1_405_55214.sh
_TRAILING_BUILD_RE = re.compile(r"_(\d{4,})$")


@dataclass(slots=True)
class ParsedInstaller:
    """Identity parsed out of a GOG installer or patch filename."""

    slug_hint: str
    version: Optional[str]
    build_id: Optional[str]
    platform: str             # windows | linux | mac
    kind: str                 # installer | patch


def parse_gog_filename(filename: str) -> Optional[ParsedInstaller]:
    """Parse a GOG installer/patch filename into its identity parts.

    Returns None for anything that is not an installer-shaped file
    (extras, artwork, sidecars) -- those are classified by the scanner,
    not guessed from the name.
    """
    if not filename:
        return None
    name = filename.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot <= 0:
        return None
    ext = name[dot + 1:].lower()
    platform = _EXT_PLATFORM.get("." + ext)
    if platform is None:
        return None

    stem = _filename_stem(name)
    if stem.startswith("gog_"):
        # Old mojosetup naming: gog_<slug>_<version>.sh
        stem = stem[len("gog_"):]
    stem = stem.strip("._")
    if not stem:
        return None

    build = _filename_build(name)
    if build is None:
        # Strip extension and part suffix, then look for a trailing
        # numeric build id (new-style .sh/.pkg naming).
        base = name[:dot].rstrip("_")
        base = re.sub(r"-\d+$", "", base)
        m = _TRAILING_BUILD_RE.search(base)
        if m:
            build = m.group(1)

    kind = "patch" if name.lower().startswith("patch_") else "installer"
    return ParsedInstaller(
        slug_hint=stem,
        version=_filename_version(name),
        build_id=build,
        platform=platform,
        kind=kind,
    )


def resolve_slug_progressive(
    slug: str,
    resolve: Callable[[str], Optional[str]],
) -> Optional[str]:
    """Resolve a slug to a store app id, stripping trailing segments.

    GOG derivative slugs (DLC folders, soundtrack pages) append suffixes
    to the base game slug; when the full slug fails, retry progressively
    shorter forms -- same approach as the GOG download handler. Each
    form is also tried with GOG's "_game" collision suffix (a directory
    named x4_foundations belongs to today's slug x4_foundations_game).
    """
    def _try(candidate: str) -> Optional[str]:
        app_id = resolve(candidate)
        if app_id:
            return app_id
        if not candidate.endswith("_game"):
            return resolve(candidate + "_game")
        return None

    app_id = _try(slug)
    if app_id:
        return app_id
    candidate = slug
    while "_" in candidate:
        candidate = candidate.rsplit("_", 1)[0]
        app_id = _try(candidate)
        if app_id:
            logger.debug("Adoption resolved slug %r via shorter form %r",
                         slug, candidate)
            return app_id
    return None


@dataclass(slots=True)
class AdoptionCandidate:
    """One file the scan proposes to adopt into the manifest."""

    absolute_path: Path
    relative_path: str        # POSIX, relative to the volume root
    filename: str
    slug: str
    store_app_id: str
    archive_type: ArchiveType
    version: Optional[str]
    build_id: Optional[str]
    size_bytes: int
    mtime: datetime


@dataclass(slots=True)
class AdoptionReport:
    """Dry-run result of an adoption scan. Nothing is written until commit()."""

    candidates: list[AdoptionCandidate] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)     # already manifested
    unmatched: list[str] = field(default_factory=list)   # could not attribute
    ignored: list[str] = field(default_factory=list)     # sidecars, hidden, links
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


class AdoptionScanner:
    """Walks the archive volume and adopts recognizable files in place.

    Args:
        manager: ArchivistManager whose volume defines root and layout.
        store_name: Store the adoption run attributes files to ("gog").
        resolve_slug: Maps a slug to a store app id, or None. The caller
            decides whether that lookup may touch the network; the
            scanner only sees the callable.
    """

    def __init__(
        self,
        manager: "ArchivistManager",
        store_name: str,
        resolve_slug: Callable[[str], Optional[str]],
    ) -> None:
        if not store_name:
            raise ValueError("store_name must not be empty")
        if not callable(resolve_slug):
            raise TypeError("resolve_slug must be callable")
        self._manager = manager
        self._store = store_name
        self._resolve = resolve_slug
        self._scan_resolve = resolve_slug  # replaced by a per-scan memo
        self._spec = self._layout_spec()

    # -- layout inversion ---------------------------------------------------

    def _layout_spec(self) -> Optional[list[re.Pattern]]:
        """Per-segment regexes inverting the volume layout for this store.

        A matched %slug%/%title%/%appid% token becomes a named group so
        the game directory identifies the game. Returns None for flat
        organization (no directory structure to invert).
        """
        volume = self._manager.volume
        org = volume.organization
        if org == "flat":
            return None
        if org == "store-slug":
            return [
                re.compile(re.escape(self._store)),
                re.compile(r"(?P<slug>[^/]+)"),
            ]
        if org == "store-title":
            return [
                re.compile(re.escape(self._store)),
                re.compile(r"(?P<title>[^/]+)"),
            ]

        # custom: substitute literal renders for the library tokens and
        # capture groups for the identity tokens, one regex per segment.
        token_res = {
            "%library%": re.escape(_sanitize_title(self._store.lower())),
            "%library_upper%": re.escape(_sanitize_title(self._store.upper())),
            "%slug_firstletter%": r"[a-z0-9]",
            "%slug%": r"(?P<slug>[^/]+)",
            "%title%": r"(?P<title>[^/]+)",
            "%appid%": r"(?P<appid>[^/]+)",
        }
        spec = []
        for segment in volume.custom_layout.split("/"):
            pattern = ""
            pos = 0
            for m in re.finditer(r"%[a-z_]+%", segment):
                pattern += re.escape(segment[pos:m.start()])
                pattern += token_res.get(m.group(0), re.escape(m.group(0)))
                pos = m.end()
            pattern += re.escape(segment[pos:])
            spec.append(re.compile(pattern))
        return spec

    def _count_game_dirs(self, base: Path) -> int:
        """Count directories at layout depth for scan progress totals.

        One scandir pass per layout level -- cheap even on large
        volumes, and it buys the progress bar a real denominator.
        Returns 0 for flat layouts (no directory structure to count).
        """
        if self._spec is None:
            return 0
        dirs = [base]
        for segment_re in self._spec:
            matched = []
            for d in dirs:
                try:
                    with os.scandir(d) as it:
                        matched.extend(
                            Path(child.path) for child in it
                            if child.is_dir(follow_symlinks=False)
                            and segment_re.fullmatch(child.name))
                except OSError:
                    continue
            dirs = matched
        return len(dirs)

    def _slug_from_parts(self, parts: tuple[str, ...]) -> Optional[dict]:
        """Match layout-depth path segments; return captured identity groups."""
        if self._spec is None or len(parts) < len(self._spec):
            return None
        groups: dict = {}
        for segment_re, part in zip(self._spec, parts):
            m = segment_re.fullmatch(part)
            if m is None:
                return None
            groups.update({k: v for k, v in m.groupdict().items() if v})
        return groups

    # -- scanning -----------------------------------------------------------

    def scan(
        self,
        root: Path,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AdoptionReport:
        """Walk root and build a dry-run adoption report.

        root must be the configured archive volume -- relative_path rows
        are only meaningful against that base. progress_cb receives
        (game_dirs_done, game_dirs_total, current_dir); total is 0 for
        flat layouts, where no denominator exists.
        """
        root = Path(root)
        base = self._manager.volume.base_path.resolve()
        if root.resolve() != base:
            raise ValueError(
                f"Adoption root {root} is not the configured archive "
                f"volume {base}")

        report = AdoptionReport()
        existing = {
            e.relative_path
            for e in self._manager.get_entries_by_store(self._store)
        }
        resolved_dirs: dict[tuple[str, ...], Optional[tuple[str, str]]] = {}
        dirs_seen = 0

        # Every unique slug is asked exactly once per scan, misses
        # included. Filename-hint fallbacks share progressive-strip
        # candidates across files, and with network lookup enabled each
        # extra call is a live API request.
        memo: dict[str, Optional[str]] = {}
        base_resolve = self._resolve

        def cached_resolve(slug: str) -> Optional[str]:
            if slug not in memo:
                memo[slug] = base_resolve(slug)
            return memo[slug]

        self._scan_resolve = cached_resolve

        total_dirs = self._count_game_dirs(base)
        if progress_cb:
            progress_cb(0, total_dirs, "")

        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            if cancel_check and cancel_check():
                report.cancelled = True
                return report

            rel_dir = Path(dirpath).relative_to(base)
            parts = () if rel_dir == Path(".") else rel_dir.parts
            depth = len(parts)

            # Prune directories that cannot belong to this store's layout
            # (sibling store folders on a shared volume stay untouched).
            if self._spec is not None and depth < len(self._spec):
                dirnames[:] = [
                    d for d in dirnames
                    if self._spec[depth].fullmatch(d)
                ]
            dirnames.sort()
            filenames.sort()

            # Ancestors of a dir at layout depth all matched their spec
            # segment (pruning above), so this counts exactly the game
            # dirs the totals pass counted.
            game_depth = len(self._spec) if self._spec is not None else 1
            if depth == game_depth:
                dirs_seen += 1
                if progress_cb:
                    progress_cb(dirs_seen, total_dirs, str(rel_dir))

            for fname in filenames:
                self._triage_file(
                    Path(dirpath) / fname, parts, existing,
                    resolved_dirs, report)

        return report

    def _triage_file(
        self,
        path: Path,
        dir_parts: tuple[str, ...],
        existing: set[str],
        resolved_dirs: dict,
        report: AdoptionReport,
    ) -> None:
        rel = str(PurePosixPath(*dir_parts, path.name))

        if path.is_symlink():
            # Adopt-in-place records where bytes live; a link is not
            # bytes, and one pointing out of the volume breaks the
            # relative_path contract outright.
            report.ignored.append(rel)
            return
        if any(part.startswith(".") for part in dir_parts):
            report.ignored.append(rel)
            return
        if rel in existing:
            report.skipped.append(rel)
            return

        dot = path.name.rfind(".")
        ext = path.name[dot + 1:].lower() if dot > 0 else ""
        if ext in IGNORED_EXTENSIONS:
            report.ignored.append(rel)
            return

        parsed = parse_gog_filename(path.name)
        if parsed is None and ext not in EXTRA_EXTENSIONS:
            report.unmatched.append(rel)
            return

        identity = self._dir_identity(dir_parts, resolved_dirs)
        if identity is None and parsed is not None:
            # No directory to lean on (flat layout or stray file):
            # fall back to the filename's own slug.
            app_id = resolve_slug_progressive(
                parsed.slug_hint, self._scan_resolve)
            if app_id:
                identity = (parsed.slug_hint, app_id)
        if identity is None:
            report.unmatched.append(rel)
            return
        slug, app_id = identity

        if parsed is not None:
            archive_type = (ArchiveType.GAME_PATCH if parsed.kind == "patch"
                            else ArchiveType.GAME_INSTALLER)
            version = parsed.version
            build_id = parsed.build_id
        else:
            archive_type = ArchiveType.GAME_EXTRA
            version = None
            build_id = None

        try:
            stat = path.stat()
        except OSError as exc:
            report.errors.append(f"{rel}: {exc}")
            return

        report.candidates.append(AdoptionCandidate(
            absolute_path=path,
            relative_path=rel,
            filename=path.name,
            slug=slug,
            store_app_id=app_id,
            archive_type=archive_type,
            version=version,
            build_id=build_id,
            size_bytes=stat.st_size,
            mtime=datetime.fromtimestamp(stat.st_mtime),
        ))

    def _dir_identity(
        self,
        dir_parts: tuple[str, ...],
        resolved_dirs: dict,
    ) -> Optional[tuple[str, str]]:
        """Slug + app id for a game directory, cached per directory."""
        if self._spec is None:
            return None
        key = dir_parts[:len(self._spec)]
        if key in resolved_dirs:
            return resolved_dirs[key]

        identity: Optional[tuple[str, str]] = None
        groups = self._slug_from_parts(key)
        if groups:
            if "appid" in groups:
                slug = groups.get("slug") or _slugify(groups.get("title", ""))
                identity = (slug or groups["appid"], groups["appid"])
            else:
                slug = groups.get("slug") or _slugify(groups.get("title", ""))
                if slug:
                    app_id = resolve_slug_progressive(slug, self._scan_resolve)
                    if app_id:
                        identity = (slug, app_id)

        resolved_dirs[key] = identity
        return identity

    # -- committing ---------------------------------------------------------

    def commit(self, report: AdoptionReport) -> int:
        """Write the report's candidates to the manifest. Returns row count."""
        if not report.candidates:
            return 0
        adopted_at = datetime.now().isoformat(timespec="seconds")
        entries = [
            ArchiveEntry(
                id=uuid.uuid4().hex,
                archive_type=c.archive_type,
                filename=c.filename,
                relative_path=c.relative_path,
                size_bytes=c.size_bytes,
                checksum_sha256="",
                downloaded_at=c.mtime,
                store_name=self._store,
                store_app_id=c.store_app_id,
                version=c.version,
                original_download_url=None,
                remote_timestamp=c.mtime,
                verified_at=None,
                metadata={
                    "adopted": True,
                    "slug": c.slug,
                    "adopted_at": adopted_at,
                },
            )
            for c in report.candidates
        ]
        self._manager.add_entries(entries)
        logger.info("Adopted %d files into the %s manifest",
                    len(entries), self._store)
        return len(entries)
