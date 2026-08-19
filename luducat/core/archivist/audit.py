# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Archive auditor -- diffs store offerings against the archive manifest.

One diff function serves both library-scale scans: run over archived
games it finds updates, run over unarchived games it finds what is
missing. "Should have" is the store's current offering filtered through
the user's default OS/language selection (core.download_selection);
"do have" is the manifest.

Matching is tiered because manifest rows differ in provenance:
entries written after provenance tracking carry the stable store
downlink in their metadata; legacy and adopted entries only have a
filename. For those the filename stem (name with version, build number
and part suffixes stripped) is compared against the store downlink's
slug.

Whether a matched game is OUTDATED is decided by build number alone
(ld-d8st): the max build extracted from the on-disk setup_* installers
against the build GOG currently serves, read off the CDN filename of
the primary installer's downlink -- resolved once per game at scan
time, cached with a TTL in store_build_cache, skipped entirely when
offline. Version strings do not get a vote; they were the source of
the ghost flags. A scan over an old archive must not cry wolf.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import urlparse

from sqlalchemy import text

from luducat.core.archivist.build_cache import (
    BuildCache,
    cache_ttl_days,
    load_builds,
)
from luducat.core.archivist.details_cache import (
    DetailsCache,
    details_cache_ttl_days,
)
from luducat.core.archivist.build_number import extract_buildnum
from luducat.core.archivist.types import (
    ArchiveEntry,
    ArchiveRequest,
    ArchiveType,
    DownloadTarget,
    GameDownloadInfo,
)
from luducat.core.download_selection import (
    _detect_platform,
    _parse_size_string,
    normalize_preferred_os,
    select_default_files,
)
from luducat.core.dt import utc_now
from luducat.core.json_compat import json

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = logging.getLogger(__name__)

try:
    _("")
except NameError:
    def _(s): return s


@dataclass(slots=True)
class AuditFile:
    """One store file the review dialog can offer for download."""

    name: str
    platform: str            # "" for extras
    language: str            # "" for extras
    version: Optional[str]
    size_bytes: Optional[int]
    downlink: str
    kind: str                # "installer" | "patch" | "extra"
    reason: str              # e.g. "new version 2.1.0.5 (archived: 2.0.0.3)"
    selected: bool           # pre-check state for the review dialog
    # Display identity of the archived release this file supersedes,
    # e.g. "2.0.0.3 (11723)"; None when nothing is archived. Defaulted
    # so payloads persisted before this field existed still load.
    archived_version: Optional[str] = None


@dataclass(slots=True)
class AuditCandidate:
    """A game with outstanding files, as produced by diff_game()."""

    store_name: str
    store_app_id: str
    game_title: str
    kind: str                # "update" | "missing"
    files: list[AuditFile]
    total_bytes: int
    # Build-number identities the update decision was made from: the
    # max on-disk setup_* build (0 = nothing usable archived) and the
    # build GOG currently serves (None = unknown/offline). Not part of
    # the persisted payload -- load_results() refills them from
    # store_build_cache.
    local_build: Optional[int] = None
    online_build: Optional[int] = None


# Extensions reveal the platform of legacy manifest rows that carry no
# platform metadata. Unknown extensions stay unmapped and are never
# platform-gated.
_EXT_PLATFORM = {
    ".exe": "windows",
    ".bin": "windows",
    ".msi": "windows",
    ".sh": "linux",
    ".pkg": "mac",
    ".dmg": "mac",
}

_PAREN_GROUP_RE = re.compile(r"\([^)]*\)")
# Dotted version tokens like 2.1.0.5, 1.32, 1.0a -- a bare build number
# such as (15215) is handled by the paren strip, not treated as version.
_VERSION_TOKEN_RE = re.compile(r"\d+(?:\.\d+)+[a-z]?")
_PART_SUFFIX_RE = re.compile(r"-\d+$")           # multi-part: -1.bin, -2.bin
_LONG_NUM_SUFFIX_RE = re.compile(r"_\d{4,}$")    # trailing build ids: _20150
_SEP_RUN_RE = re.compile(r"[_\-]{2,}")


def _strip_extension(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[:dot] if dot > 0 else base


def _filename_stem(filename: str) -> str:
    """Reduce an installer filename to its stable game stem.

    setup_teenagent_2.1.0.5_(15215).exe -> teenagent
    setup_baldurs_gate_2.5.16.4_(23121)-1.bin -> baldurs_gate
    """
    s = _strip_extension(filename.lower())
    s = _PAREN_GROUP_RE.sub("", s)
    s = _VERSION_TOKEN_RE.sub("", s)
    while True:
        stripped = s.rstrip("_-")
        stripped = _PART_SUFFIX_RE.sub("", stripped)
        # Long trailing digit runs are build ids; short ones ("witcher_3",
        # the mojosetup "_2" counter) belong to the name and stay.
        stripped = _LONG_NUM_SUFFIX_RE.sub("", stripped)
        if stripped == s:
            break
        s = stripped
    s = _SEP_RUN_RE.sub("_", s).strip("_-")
    for prefix in ("setup_", "patch_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s


def _filename_version(filename: str) -> Optional[str]:
    """Version encoded in an installer filename, if recognizable.

    Takes the last dotted token so patch names like
    patch_x_2.0.0.3_2.1.0.5_(...).exe yield the target version.
    """
    s = _PAREN_GROUP_RE.sub("", _strip_extension(filename))
    matches = _VERSION_TOKEN_RE.findall(s)
    return matches[-1] if matches else None


_BUILD_NUM_RE = re.compile(r"\((\d+)\)")


def _filename_build(filename: str) -> Optional[str]:
    """GOG build id encoded in an installer filename: setup_x_1.0_(15215).exe.

    The parenthesized number identifies the release precisely; the dotted
    version is the fallback identity (same logic as gog-cleanup-archive).
    Plausible builds have 4+ digits; shorter groups (browser duplicate
    suffixes like "(1)") are ignored when a plausible one exists.
    """
    matches = _BUILD_NUM_RE.findall(filename)
    if not matches:
        return None
    plausible = [m for m in matches if len(m) >= 4]
    return plausible[-1] if plausible else matches[-1]


def _clean_store_version(version: Optional[str]) -> Optional[str]:
    """Trim GOG's version_name to its leading version token for display.

    Repackaged games carry version_name = version[_buildname[_buildid]],
    e.g. "6.45.1_TheSwarm_170671" or "1.7_alttab_hotfix". Only the
    leading token is a real version; the rest is build metadata that the
    build id already captures. Left untouched when the leading segment
    has no digit (no numeric version to isolate). Display only --
    comparison uses the dotted core via _version_tuple regardless.
    """
    if not version or "_" not in version:
        return version
    head = version.split("_", 1)[0]
    return head if any(c.isdigit() for c in head) else version


def file_display_version(version: Optional[str],
                         filename: Optional[str]) -> str:
    """Human display of a file's release identity: version, build, or both.

    "2.1.0.5 (15215)" when both are known, bare build when GOG reports
    no version string, empty when neither is recoverable.
    """
    build = _filename_build(filename) if filename else None
    version = _clean_store_version(
        version or (_filename_version(filename) if filename else None))
    if version and build:
        return f"{version} ({build})"
    if version:
        return version
    if build:
        return f"({build})"
    return ""


_DOTTED_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


def version_pseudo_build(version: str) -> Optional[int]:
    """Dotted version -> synthetic build number by summing the parts.

    2.2.0.26 -> 30. Same heuristic as gog-cleanup-archive's
    extract_buildnum. Only meaningful when ordering a dotted-only
    release against a real GOG build id (which dwarfs any sum); for
    dotted-vs-dotted use _version_tuple, the sum misorders there
    (2.5.16.4 sums higher than 2.6.0.0).
    """
    if not version:
        return None
    m = _DOTTED_VERSION_RE.search(version)
    if not m:
        return None
    return sum(int(part) for part in m.group(0).split("."))


def _version_tuple(version: str) -> Optional[tuple[int, ...]]:
    """Dotted version core as a comparable tuple: "2.6.0.0" -> (2, 6, 0, 0)."""
    if not version:
        return None
    m = _DOTTED_VERSION_RE.search(version)
    if not m:
        return None
    return tuple(int(part) for part in m.group(0).split("."))


def release_display(identity: Optional[str], build_mode: bool) -> str:
    """Render a stored release identity for the review dialog.

    identity is either a file_display_version output ("2.1.0.5 (15215)")
    or a raw store version straight from the New column (f.version), so
    it gets the same version_name trim as everywhere else. Version mode
    shows it as stored; build mode prefers the build id and falls back to
    the pseudo-build computed from the dotted version (gog-cleanup
    semantics).
    """
    if not identity:
        return ""
    identity = _clean_store_version(identity)
    if not build_mode:
        return identity
    build = _filename_build(identity)
    if build:
        return build
    pseudo = version_pseudo_build(identity)
    return str(pseudo) if pseudo is not None else identity


def _filename_platform(filename: str) -> Optional[str]:
    dot = filename.rfind(".")
    if dot < 0:
        return None
    return _EXT_PLATFORM.get(filename[dot:].lower())


def _downlink_path(downlink: str) -> str:
    """Normalize absolute and relative downlinks to the URL path."""
    if not downlink:
        return ""
    if "://" in downlink:
        return urlparse(downlink).path
    return downlink


def _downlink_slug(downlink: str) -> str:
    """Game slug segment of a downlink: /downloads/<slug>/<file_id>."""
    segments = [seg for seg in _downlink_path(downlink).split("/") if seg]
    if len(segments) < 2:
        return ""
    return segments[-2]


_TRAILING_DIGITS_RE = re.compile(r"(\d+)$")


def _downlink_part(downlink: str) -> Optional[int]:
    """Sequence number at the end of a downlink's file segment.

    GOG numbers a release's files: en1installer0 is the exe,
    en1installer3 the -3.bin part. It is the only per-file identity a
    wanted item carries (gameDetails has no filenames). None when the
    segment has no trailing digits.
    """
    segments = [seg for seg in _downlink_path(downlink).split("/") if seg]
    if not segments:
        return None
    m = _TRAILING_DIGITS_RE.search(segments[-1])
    return int(m.group(1)) if m else None


def _filename_part(filename: str) -> int:
    """Part number encoded in an installer filename: -3.bin -> 3, .exe -> 0."""
    m = _PART_SUFFIX_RE.search(_strip_extension(filename))
    return int(m.group(0)[1:]) if m else 0


def _entry_release_key(entry: ArchiveEntry) -> tuple:
    """Ordering key so the newest of several same-downlink rows wins.

    Updating never deletes the superseded installer from the archive,
    so a downlink can map to more than one manifest row. Download time
    orders them; the version tuple breaks ties.
    """
    stamp = entry.downloaded_at or datetime.min
    version = _version_tuple(
        entry.version or _filename_version(entry.filename) or "")
    return (stamp, version or ())


def _judge_entry(
    entry: ArchiveEntry,
    game_outdated: bool,
    online_build: Optional[int],
) -> tuple[bool, str]:
    """Judge a matched manifest entry by build identity, nothing else.

    When the game is not outdated (its on-disk build already matches or
    beats what GOG serves) every matched entry is satisfied -- version
    strings do not get a vote, that is where the ghost flags came from.
    When it is outdated, an entry only counts if it IS the new release
    (its filename carries the online build id, e.g. a part that was
    already re-downloaded); everything else is stale and offered.
    """
    if not game_outdated:
        return True, ""
    if online_build is not None and extract_buildnum(entry.filename) == online_build:
        return True, ""
    return False, _("newer build {new} (archived: {old})").format(
        new=online_build,
        old=file_display_version(entry.version, entry.filename) or "?")


def _compare(item: dict[str, Any], entry: ArchiveEntry) -> tuple[bool, str]:
    """Decide whether a matched manifest entry satisfies the wanted item.

    Version-string comparison, kept ONLY for prune_stale_results()'s
    DB-only recheck after downloads complete (the fresh manifest row
    carries exactly the wanted version). The scan-time update decision
    does not use this -- it goes through _judge_entry and the build
    numbers (ld-d8st).

    Returns (satisfied, reason). Legacy rows have no version column, so
    the version encoded in the filename stands in. With nothing
    comparable on either side the entry is NOT treated as satisfied
    (lud-o50c): the scan flagged this game for a reason (build diff),
    and the prune cannot confirm the download happened without positive
    evidence. The next scan will clear the game if the build is current.
    """
    wanted_version = item.get("version")
    entry_version = entry.version or _filename_version(entry.filename)
    if wanted_version and entry_version:
        if str(wanted_version) == str(entry_version):
            return True, ""
        wanted_tuple = _version_tuple(str(wanted_version))
        entry_tuple = _version_tuple(str(entry_version))
        if (wanted_tuple is not None and entry_tuple is not None
                and wanted_tuple <= entry_tuple):
            return True, ""
        return False, _("new version {new} (archived: {old})").format(
            new=_clean_store_version(str(wanted_version)),
            old=file_display_version(entry.version, entry.filename))
    wanted_filename = item.get("filename")
    if wanted_filename and entry.filename and wanted_filename != entry.filename:
        return False, _("file changed (archived: {old})").format(
            old=entry.filename)
    return False, ""


def _tokens_equal(a: str, b: str) -> bool:
    """Token equality tolerant of GOG's trademark transliteration.

    "Dungeon Keeper(TM) 2" ships as setup_dungeon_keepertm_2_...: the
    U+2122 trademark NFKD-decomposes to "TM" and glues onto the previous
    token in the filename, while the download slug drops it. So
    "keepertm" and "keeper" name the same token.
    """
    if a == b:
        return True
    return a == b + "tm" or b == a + "tm"


def _is_token_prefix(short: list[str], long: list[str]) -> bool:
    """True when every token of `short` matches the head of `long`."""
    if len(short) > len(long):
        return False
    return all(_tokens_equal(s, long[i]) for i, s in enumerate(short))


def _stems_match(entry_stem: str, wanted_stem: str, slug: str) -> bool:
    if wanted_stem and entry_stem == wanted_stem:
        return True
    if not slug:
        return False
    # The slug and the filename stem must share a token prefix (either
    # direction): Linux installers append language/counter tokens to the
    # slug (beneath_a_steel_sky_en_gog_2), and repackaged installers
    # append build-name tokens to the filename (dungeon_keeper_2 vs
    # dungeon_keepertm_2_alttab_hotfix). Comparison is token-wise with
    # trademark tolerance. The manifest passed in is already scoped to
    # one game, so cross-game collisions cannot happen here.
    entry_tokens = entry_stem.split("_")
    slug_tokens = slug.split("_")
    return (_is_token_prefix(slug_tokens, entry_tokens)
            or _is_token_prefix(entry_tokens, slug_tokens))


def _wanted_part_ranks(wanted: list[dict]) -> dict[int, int]:
    """Map each wanted item (by id) to its part rank within its group.

    GOG's downlink index (en1installer0/1/2) is positional, not a part
    number: plenty of single-file games have their exe at index 1 or 2.
    Only the ORDER within one product's (platform, language) group is
    meaningful -- the first file is the exe (part 0), the rest map to
    -1.bin, -2.bin. The product is the downlink slug: addon installers
    live on their own slug with an index sequence restarting at 0, and
    ranking them into the base game's group shifts every later part up
    (the MaSzyna/Portia tail-part ghosts, 2026-07-12). Items whose
    downlink has no index rank None (unfiltered).
    """
    groups: dict[tuple, list[tuple[int, int]]] = {}
    for idx, item in enumerate(wanted):
        part = _downlink_part(item.get("downlink", ""))
        if part is None:
            continue
        key = (_downlink_slug(item.get("downlink", "")),
               item.get("platform") or "",
               (item.get("language") or "").lower())
        groups.setdefault(key, []).append((part, idx))
    ranks: dict[int, int] = {}
    for members in groups.values():
        for rank, (__, idx) in enumerate(sorted(members)):
            ranks[idx] = rank
    return ranks


def _match_wanted(
    item: dict[str, Any],
    by_downlink: dict[str, ArchiveEntry],
    by_filename: dict[str, ArchiveEntry],
    installer_entries: list[ArchiveEntry],
    wanted_part: Optional[int] = None,
    game_outdated: bool = False,
    local_build: Optional[int] = None,
    online_build: Optional[int] = None,
) -> tuple[bool, str, Optional[str]]:
    """Find the manifest match for a wanted item and judge it.

    Tiers: stable downlink (provenance-tracked rows), exact filename,
    stem match for legacy/adopted rows, and finally build identity --
    a part/platform/language-filtered candidate whose filename carries
    one of the game's known builds IS this file, however far the names
    drifted (renamed slugs, "(gog-N)" store versions, repack naming;
    the lud-dxu5 shapes). The reference build comes from the per-game
    cache, so this tier costs nothing -- it replaces the per-file CDN
    resolution the old identity tier spent one request per flagged
    file on. Identity only, never ordering: an outdated game accepts
    only the online build (a file already re-downloaded), an
    up-to-date game accepts its local build too. Items unmatched by
    every tier are "not archived" regardless of builds -- a missing
    part of an up-to-date game still flags.

    wanted_part is the item's rank within its platform/language group
    (see _wanted_part_ranks), None when unknown.
    Returns (satisfied, reason, archived_display) -- archived_display
    names the superseded release when a stale match exists, None
    otherwise.
    """
    entry = by_downlink.get(_downlink_path(item.get("downlink", "")))
    if entry is not None:
        satisfied, reason = _judge_entry(entry, game_outdated, online_build)
        if satisfied:
            return True, "", None
        return False, reason, file_display_version(entry.version, entry.filename)

    wanted_filename = item.get("filename") or ""
    if wanted_filename and wanted_filename in by_filename:
        # The store offers the exact file that is archived; identity
        # cannot get stronger than that, builds do not get a vote.
        return True, "", None

    slug = _downlink_slug(item.get("downlink", ""))
    wanted_stem = _filename_stem(wanted_filename) if wanted_filename else ""
    wanted_platform = item.get("platform") or ""
    wanted_language = (item.get("language") or "").lower()

    filtered: list[ArchiveEntry] = []
    for candidate in installer_entries:
        entry_platform = _filename_platform(candidate.filename)
        if entry_platform and wanted_platform and entry_platform != wanted_platform:
            continue
        entry_language = ((candidate.metadata or {}).get("language") or "").lower()
        if entry_language and wanted_language and entry_language != wanted_language:
            continue
        # Same-stem is not enough identity for multi-part releases: two
        # stray .bin parts of a half-finished download must not mark the
        # other six files as archived. The downlink sequence number maps
        # onto the filename's -N part suffix (exe = 0).
        if (wanted_part is not None
                and _filename_part(candidate.filename) != wanted_part):
            continue
        filtered.append(candidate)

    first_miss: Optional[str] = None
    first_display: Optional[str] = None
    matched_any = False
    for candidate in filtered:
        if not _stems_match(_filename_stem(candidate.filename), wanted_stem, slug):
            continue
        matched_any = True
        satisfied, reason = _judge_entry(candidate, game_outdated, online_build)
        if satisfied:
            return True, "", None
        if first_miss is None:
            first_miss = reason
            first_display = file_display_version(
                candidate.version, candidate.filename)

    # Build-identity tier. Build 0 never vouches -- "no recoverable
    # build" matching "no recoverable build" is not an identity.
    if game_outdated:
        accepted = {online_build} if online_build else set()
    else:
        accepted = {b for b in (online_build, local_build) if b}
    if filtered and accepted:
        for candidate in filtered:
            if extract_buildnum(candidate.filename) in accepted:
                return True, "", None

    if matched_any:
        return False, first_miss or _("not archived"), first_display
    return False, _("not archived"), None


def diff_game(
    info: GameDownloadInfo,
    manifest: list[ArchiveEntry],
    preferred_os: list[str],
    preferred_languages: list[str],
    kind: str,
    include_patches: bool = True,
    wanted: Optional[list[dict[str, Any]]] = None,
    local_build: Optional[int] = None,
    online_build: Optional[int] = None,
) -> Optional[AuditCandidate]:
    """Diff one game's current offerings against its manifest entries.

    The update decision is the build-number compare: the game is
    outdated when online_build > local_build, strictly (never offer a
    downgrade; a repack with a lower build does not flag). Matched
    files of an up-to-date game are satisfied whatever their version
    strings say; unmatched files flag as "not archived" either way, so
    missing parts surface without any build knowledge.

    kind="update" appends the game's patches, kind="missing" appends its
    extras -- both only when at least one default installer is
    outstanding; a fully satisfied game returns None either way.
    include_patches only sets the patches' pre-check state; the rows
    are in the payload either way because the review dialog re-derives
    both their visibility and check state from the live setting --
    toggling it must work without a rescan. wanted, when given, is the
    precomputed select_default_files() output (the scan already needed
    it for the primary downlink).
    """
    if kind not in ("update", "missing"):
        raise ValueError(f"invalid audit kind: {kind!r}")

    if wanted is None:
        wanted = select_default_files(info, preferred_os, preferred_languages)
    game_outdated = (online_build is not None
                     and online_build > (local_build or 0))

    installer_entries: list[ArchiveEntry] = []
    by_downlink: dict[str, ArchiveEntry] = {}
    all_downlink_paths: set[str] = set()
    for e in manifest:
        is_installer = e.archive_type in (
            ArchiveType.GAME_INSTALLER, ArchiveType.GAME_DLC)
        if is_installer:
            installer_entries.append(e)
        path = _downlink_path((e.metadata or {}).get("downlink", ""))
        if not path:
            continue
        all_downlink_paths.add(path)
        if is_installer:
            held = by_downlink.get(path)
            if held is None or _entry_release_key(e) > _entry_release_key(held):
                by_downlink[path] = e
    by_filename = {e.filename: e for e in installer_entries}

    part_ranks = _wanted_part_ranks(wanted)

    files: list[AuditFile] = []
    for idx, item in enumerate(wanted):
        satisfied, reason, archived_display = _match_wanted(
            item, by_downlink, by_filename, installer_entries,
            wanted_part=part_ranks.get(idx),
            game_outdated=game_outdated,
            local_build=local_build, online_build=online_build)
        if satisfied:
            continue
        files.append(AuditFile(
            name=item.get("name", ""),
            platform=item.get("platform") or "",
            language=item.get("language") or "",
            version=item.get("version"),
            size_bytes=_parse_size_string(item.get("size") or ""),
            downlink=item.get("downlink", ""),
            kind="installer",
            reason=reason,
            selected=True,
            archived_version=archived_display,
        ))

    if not files:
        return None

    if kind == "update":
        optional_items = info.patches
        optional_kind = "patch"
        optional_selected = include_patches
    else:
        optional_items = info.extras
        optional_kind = "extra"
        optional_selected = False
    for item in optional_items:
        path = _downlink_path(item.get("downlink", ""))
        if path and path in all_downlink_paths:
            continue
        files.append(AuditFile(
            name=item.get("name", ""),
            platform=item.get("platform") or "",
            language=item.get("language") or "",
            version=item.get("version"),
            size_bytes=_parse_size_string(item.get("size") or ""),
            downlink=item.get("downlink", ""),
            kind=optional_kind,
            reason="",
            selected=optional_selected,
        ))

    total_bytes = sum(f.size_bytes or 0 for f in files if f.selected)
    return AuditCandidate(
        store_name=info.store_name,
        store_app_id=info.store_app_id,
        game_title=info.game_title,
        kind=kind,
        files=files,
        total_bytes=total_bytes,
        local_build=local_build,
        online_build=online_build,
    )


# -- Enqueue ------------------------------------------------------------------

_KIND_TO_TYPE = {
    "installer": ArchiveType.GAME_INSTALLER,
    "patch": ArchiveType.GAME_PATCH,
    "extra": ArchiveType.GAME_EXTRA,
}

_PLATFORM_EXT = {"windows": ".exe", "linux": ".sh", "mac": ".pkg"}

_UNSAFE_NAME_RE = re.compile(r"[^\w.-]+")


def _placeholder_filename(file: AuditFile) -> str:
    """Deterministic stand-in name until the CDN redirect reveals the real one.

    Built from the downlink's slug and file id so multi-file games get
    distinct names; the extension is guessed from the platform.
    """
    slug = _downlink_slug(file.downlink)
    tail = _downlink_path(file.downlink).rsplit("/", 1)[-1]
    base = "_".join(p for p in (slug, tail) if p)
    if not base:
        base = _UNSAFE_NAME_RE.sub("_", file.name.lower()).strip("_")
    return base + _PLATFORM_EXT.get(file.platform, "")


def build_target(
    candidate: AuditCandidate,
    selected: list[AuditFile],
) -> DownloadTarget:
    """Lazy DownloadTarget: url='' + downlink metadata per file (Phase B).

    No network calls here -- the download manager resolves each file's
    downlink to a CDN URL at worker start. Cookies are deliberately not
    baked in; the lazy resolve fetches fresh ones so a queue that sits
    for hours never carries stale session data.
    """
    if not selected:
        raise ValueError("build_target needs at least one selected file")

    files: list[ArchiveRequest] = []
    for f in selected:
        if not f.downlink:
            raise ValueError(
                f"audit file {f.name!r} has no downlink, cannot enqueue lazily")
        files.append(ArchiveRequest(
            url="",
            archive_type=_KIND_TO_TYPE.get(f.kind, ArchiveType.GAME_EXTRA),
            store_name=candidate.store_name,
            store_app_id=candidate.store_app_id,
            game_title=candidate.game_title,
            filename=_placeholder_filename(f),
            expected_size=f.size_bytes,
            version=f.version,
            metadata={"downlink": f.downlink, "language": f.language},
        ))

    return DownloadTarget(
        game_title=candidate.game_title,
        store_name=candidate.store_name,
        store_app_id=candidate.store_app_id,
        files=files,
    )


# -- Disk reconciliation ------------------------------------------------------


def _configured_archive_root(config) -> Optional["Path"]:
    """Archive root for disk checks, or None when there is none to trust.

    Only an explicitly configured downloads.archive_path counts -- the
    factory default is where the downloader would write, not evidence
    the user keeps an archive there. A configured but unreachable path
    (unmounted volume) also yields None: flagging a whole library for
    redownload because /storage is not mounted would be far worse than
    trusting the manifest for one scan.
    """
    if config is None:
        return None
    raw = config.get("downloads.archive_path", "")
    if not raw:
        return None
    root = Path(raw)
    if not root.is_dir():
        logger.warning(
            "Archive path %s not accessible -- audit trusts the manifest "
            "for this scan", raw)
        return None
    return root


def reconcile_with_disk(
    entries: list[ArchiveEntry], root: Optional["Path"],
) -> list[ArchiveEntry]:
    """Correct one game's manifest rows against the filesystem (ld-f06z).

    The manifest claims, the disk decides: rows whose file is gone are
    dropped (the matcher then re-flags them), and installer-shaped
    files sitting in the game's directories without a manifest row are
    synthesized as entries so they can satisfy the audit. Pickup
    follows the cleanup-script rules: setup_-prefixed exe/bin (mac and
    linux installers keep their native naming), never patches, never
    extras -- parse_gog_filename is the gatekeeper. Flat layouts get
    the existence check only; listing the volume root would sweep
    other games' files into this game's scope.
    """
    if root is None or not entries:
        return entries

    # Local import: adoption imports the filename helpers from this
    # module, a top-level import here would be circular.
    from luducat.core.archivist.adoption import parse_gog_filename

    kept: list[ArchiveEntry] = []
    game_dirs: set[str] = set()
    known_names: set[str] = set()
    for entry in entries:
        rel = PurePosixPath(entry.relative_path)
        known_names.add(entry.filename)
        parent = str(rel.parent)
        if parent != ".":
            game_dirs.add(parent)
        if (root / rel).is_file():
            kept.append(entry)
        else:
            logger.info("Archived file missing on disk, re-flagging: %s",
                        entry.relative_path)

    template = entries[0]
    for rel_dir in sorted(game_dirs):
        dir_path = root / PurePosixPath(rel_dir)
        if not dir_path.is_dir():
            continue
        for path in sorted(dir_path.iterdir()):
            if not path.is_file() or path.name in known_names:
                continue
            parsed = parse_gog_filename(path.name)
            if parsed is None or parsed.kind != "installer":
                continue
            if (parsed.platform == "windows"
                    and not path.name.lower().startswith("setup_")):
                continue
            stat = path.stat()
            kept.append(ArchiveEntry(
                id=f"disk:{rel_dir}/{path.name}",
                archive_type=ArchiveType.GAME_INSTALLER,
                filename=path.name,
                relative_path=f"{rel_dir}/{path.name}",
                size_bytes=stat.st_size,
                checksum_sha256="",
                downloaded_at=datetime.fromtimestamp(stat.st_mtime),
                store_name=template.store_name,
                store_app_id=template.store_app_id,
            ))
    return kept


# -- Build numbers (ld-d8st) --------------------------------------------------


def local_build_from_manifest(
    entries: list[ArchiveEntry],
    preferred_os: list[str],
) -> int:
    """MAX build over a game's primary on-disk installers, 0 fallback.

    gog-cleanup-archive.py semantics: only setup_-prefixed primary
    installers of the wanted OS count (.exe / .sh / .pkg), never
    patch_* and never the -N.bin continuation parts -- those belong to
    the exe and carry the same build id. Runs over the reconciled
    manifest, so the filenames ARE the on-disk names (rows whose file
    vanished were dropped, unadopted installers were synthesized).
    Mac and Linux installers keep GOG's native naming without the
    setup_ prefix, so the prefix rule applies to Windows only --
    parse_gog_filename gatekeeps the rest, same as reconcile_with_disk.

    0 means nothing usable is archived: no entries, no installer, or
    none for the wanted OS. The compare treats 0 as "any online build
    is newer".
    """
    exts = {_PLATFORM_EXT[o]
            for o in normalize_preferred_os(preferred_os)
            if o in _PLATFORM_EXT}
    if not entries or not exts:
        return 0

    # Local import: adoption imports filename helpers from this module.
    from luducat.core.archivist.adoption import parse_gog_filename

    best = 0
    for e in entries:
        if e.archive_type not in (ArchiveType.GAME_INSTALLER,
                                  ArchiveType.GAME_DLC):
            continue
        name = e.filename or ""
        low = name.lower()
        dot = low.rfind(".")
        if dot <= 0 or low[dot:] not in exts:
            continue
        if low.startswith("patch_"):
            continue
        if low.endswith(".exe") and not low.startswith("setup_"):
            continue
        parsed = parse_gog_filename(name)
        if parsed is None or parsed.kind != "installer":
            continue
        best = max(best, extract_buildnum(name))
    return best


def primary_installer_downlink(wanted: list[dict[str, Any]]) -> Optional[str]:
    """Downlink of the game's primary installer (the exe/sh/pkg).

    The first item ranked 0 within its slug/platform/language group --
    gameDetails lists the base product first, so this is the base
    game's exe, not an addon's. Items whose downlinks carry no index
    fall back to plain first-with-a-downlink. The online build check
    resolves exactly this one downlink; the -N.bin parts share its
    build id and are never resolved.
    """
    ranks = _wanted_part_ranks(wanted)
    for idx, item in enumerate(wanted):
        if ranks.get(idx) == 0 and item.get("downlink"):
            return item["downlink"]
    for item in wanted:
        if item.get("downlink"):
            return item["downlink"]
    return None


def _network_online() -> bool:
    """Offline detection, same source as the metadata resolver."""
    try:
        from luducat.core.network_monitor import get_network_monitor
        return get_network_monitor().is_online
    except RuntimeError:
        return True  # monitor not initialized (tests, headless) -- assume online


# -- Scan orchestration -------------------------------------------------------

_INSTALLER_TYPES = (ArchiveType.GAME_INSTALLER.value, ArchiveType.GAME_DLC.value)


class ArchiveAuditor:
    """Diffs the archive manifest against current store offerings.

    scan_updates(): archived games whose default file set is stale.
    scan_missing(): owned games with no installer entry in the manifest.
    Both run synchronously -- callers wrap them in a worker thread.
    Per-game store failures are collected in ``errors`` and never abort
    a scan. A completed scan replaces the persisted (store, kind)
    results; a cancelled one merges what it reached into them, so an
    earlier full scan survives the cancellation.
    """

    def __init__(self, engine: "Engine", handler, config) -> None:
        if engine is None or handler is None:
            raise ValueError("ArchiveAuditor needs an engine and a handler")
        self._engine = engine
        self._handler = handler
        self._config = config
        self.errors: list[str] = []

    def scan_updates(
        self,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        force_refresh: bool = False,
    ) -> list[AuditCandidate]:
        store = self._handler.store_name
        with self._engine.connect() as conn:
            archived = [row[0] for row in conn.execute(
                text(
                    "SELECT DISTINCT store_app_id FROM archives "
                    "WHERE store_name = :store "
                    "AND archive_type IN ('game_installer', 'game_dlc') "
                    "AND store_app_id IS NOT NULL AND store_app_id != '' "
                    "ORDER BY store_app_id"
                ),
                {"store": store},
            ).fetchall()]
            titles = self._owned_titles(conn, store)
        # An adopted archive can hold far more games than the account
        # licenses (other sources, revoked licenses, DLC product ids).
        # gameDetails is account-scoped and answers empty for those, so
        # checking them is a request wasted by design -- skip them and
        # say so once, not once per game.
        targets = [(app_id, titles[app_id])
                   for app_id in archived if app_id in titles]
        unlicensed = len(archived) - len(targets)
        if unlicensed:
            # Log only -- an expected property of an adopted archive,
            # not something to bother the user with after every scan.
            logger.info(
                "Update scan: skipping %d archived %s games without a "
                "license on this account", unlicensed, store)
        return self._scan("update", targets, progress_cb, cancel_check,
                          force_refresh=force_refresh)

    def scan_missing(
        self,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        force_refresh: bool = False,
    ) -> list[AuditCandidate]:
        store = self._handler.store_name
        with self._engine.connect() as conn:
            archived = {row[0] for row in conn.execute(
                text(
                    "SELECT DISTINCT store_app_id FROM archives "
                    "WHERE store_name = :store "
                    "AND archive_type IN ('game_installer', 'game_dlc') "
                    "AND store_app_id IS NOT NULL"
                ),
                {"store": store},
            ).fetchall()}
            titles = self._owned_titles(conn, store)
        targets = [
            (app_id, title)
            for app_id, title in sorted(titles.items(), key=lambda t: t[1])
            if app_id not in archived
        ]
        return self._scan("missing", targets, progress_cb, cancel_check,
                          force_refresh=force_refresh)

    def _owned_titles(self, conn, store: str) -> dict[str, str]:
        rows = conn.execute(
            text(
                "SELECT sg.store_app_id, g.title FROM store_games sg "
                "JOIN games g ON g.id = sg.game_id "
                "WHERE sg.store_name = :store"
            ),
            {"store": store},
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def _preferences(self) -> tuple[list[str], list[str]]:
        return _current_preferences(self._config)

    def _include_patches(self) -> bool:
        if self._config is None:
            return True
        return bool(self._config.get("downloads.download_patches", True))

    def _load_manifest(self, store: str, app_id: str) -> list[ArchiveEntry]:
        return _load_manifest_rows(self._engine, store, app_id)

    def _game_builds(
        self,
        app_id: str,
        wanted: list[dict],
        manifest: list[ArchiveEntry],
        preferred_os: list[str],
        cache: BuildCache,
        online_mode: bool,
    ) -> tuple[int, Optional[int]]:
        """(local, online) build for one game, cache-first (ld-d8st).

        Local comes from the reconciled manifest, online from resolving
        the primary installer's downlink ONCE and reading the build id
        off the CDN filename -- the only namespace-correct source; the
        gameDetails version is a display string and the content-system
        API serves Galaxy depot builds. Offline never resolves and
        serves whatever the cache holds, stale included. A failed
        resolution keeps the previous cached answer instead of
        poisoning the cache for a whole TTL.
        """
        local = cache.local_build(app_id)
        if local is None:
            local = local_build_from_manifest(manifest, preferred_os)
            cache.set_local(app_id, local)
        if not online_mode:
            return local, cache.online_build(app_id, allow_stale=True)
        online = cache.online_build(app_id)
        if online is None:
            online = self._resolve_online_build(wanted)
            if online is not None:
                cache.set_online(app_id, online)
            else:
                online = cache.online_build(app_id, allow_stale=True)
        return local, online

    def _resolve_online_build(self, wanted: list[dict]) -> Optional[int]:
        """Build id GOG currently serves, from one downlink resolution."""
        refresh = getattr(self._handler, "refresh_download_url", None)
        if refresh is None:
            return None
        downlink = primary_installer_downlink(wanted)
        if not downlink:
            return None
        from luducat.core.download_handlers.base import (
            extract_filename_from_cdn_url,
        )
        try:
            result = refresh({"downlink": downlink})
        except Exception as e:
            logger.debug("Online build lookup failed for %s: %s", downlink, e)
            return None
        if not result:
            return None
        cdn_name = extract_filename_from_cdn_url(result[0])
        if not cdn_name:
            return None
        return extract_buildnum(cdn_name)

    def _scan(
        self,
        kind: str,
        targets: list[tuple[str, str]],
        progress_cb: Optional[Callable[[int, int, str], None]],
        cancel_check: Optional[Callable[[], bool]],
        force_refresh: bool = False,
    ) -> list[AuditCandidate]:
        store = self._handler.store_name
        preferred_os, preferred_languages = self._preferences()
        include_patches = self._include_patches()
        archive_root = _configured_archive_root(self._config)
        # Build identities drive the update decision (ld-d8st); the
        # missing scan diffs games without manifest rows, where builds
        # have nothing to say.
        build_cache: Optional[BuildCache] = None
        if kind == "update":
            build_cache = BuildCache(
                self._engine, store, cache_ttl_days(self._config))
            build_cache.load()
        det_cache = DetailsCache(
            self._engine, store,
            details_cache_ttl_days(self._config))
        if force_refresh:
            det_cache.invalidate_all()
        det_cache.load()
        online_mode = _network_online()
        self.errors = []
        candidates: list[AuditCandidate] = []
        scanned: list[str] = []
        cancelled = False
        cached_count = 0
        fetched_count = 0
        total = len(targets)

        for done, (app_id, title) in enumerate(targets, start=1):
            if cancel_check is not None and cancel_check():
                logger.info("Audit scan (%s) cancelled after %d/%d games",
                            kind, done - 1, total)
                cancelled = True
                break
            was_cached = det_cache.get(app_id) is not None
            try:
                info = self._handler.get_available_downloads(
                    app_id, details_cache=det_cache)
                if cancel_check is not None and cancel_check():
                    # Cancelled while the request was in flight -- do not
                    # let this game's result sneak into a cancelled scan.
                    logger.info("Audit scan (%s) cancelled after %d/%d games",
                                kind, done - 1, total)
                    cancelled = True
                    break
                manifest = reconcile_with_disk(
                    self._load_manifest(store, app_id), archive_root)
                wanted = select_default_files(
                    info, preferred_os, preferred_languages)
                local_build: Optional[int] = None
                online_build: Optional[int] = None
                if build_cache is not None:
                    local_build, online_build = self._game_builds(
                        app_id, wanted, manifest, preferred_os,
                        build_cache, online_mode)
                candidate = diff_game(
                    info, manifest, preferred_os, preferred_languages, kind,
                    include_patches=include_patches, wanted=wanted,
                    local_build=local_build, online_build=online_build)
                scanned.append(app_id)
                if candidate is not None:
                    candidates.append(candidate)
                if was_cached:
                    cached_count += 1
                else:
                    fetched_count += 1
            except Exception as e:
                # One delisted or broken product must not kill a
                # library-scale scan; the UI summarizes the misses.
                # Not counted as scanned: a previous scan's knowledge of
                # this game beats no knowledge at all.
                logger.warning("Audit scan (%s) failed for %s (%s): %s",
                               kind, title, app_id, e)
                self.errors.append(f"{title}: {e}")
            if progress_cb is not None:
                progress_cb(done, total, title)

        det_cache.flush()
        if cached_count or fetched_count:
            logger.info(
                "Audit scan (%s): %d cached, %d fetched from API",
                kind, cached_count, fetched_count)
        if build_cache is not None:
            build_cache.flush()

        if cancelled or self.errors:
            # Cancelled: fresh knowledge only exists for the games the
            # scan reached. Errored: per-game exceptions must not erase
            # the previous scan's knowledge of those games (lud-elwf).
            # Either way, merge replaces only the scanned app_ids and
            # preserves everything else.
            merge_results(self._engine, store, kind, scanned, candidates)
            if not cancelled:
                record_scan_preferences(
                    self._engine, store, kind,
                    preferred_os, preferred_languages)
        else:
            replace_results(self._engine, store, kind, candidates)
            record_scan_preferences(
                self._engine, store, kind,
                preferred_os, preferred_languages)
        return candidates


def _load_manifest_rows(
    engine: "Engine", store: str, app_id: str,
) -> list[ArchiveEntry]:
    # Local import: manager pulls in VolumeManager, which the auditor
    # does not need for anything else.
    from luducat.core.archivist.manager import _row_to_entry

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT * FROM archives "
                "WHERE store_name = :store AND store_app_id = :app"
            ),
            {"store": store, "app": app_id},
        ).mappings().all()
    return [_row_to_entry(r) for r in rows]


def prune_stale_results(engine: "Engine", store_name: str, kind: str) -> int:
    """Drop persisted audit files the archive meanwhile satisfies.

    Cheap DB-only recheck (no store traffic) so badge counts follow
    completed downloads instead of freezing at scan time; the scan
    itself stays the authority for discovering NEW work. A game whose
    selected files are all satisfied disappears from the results.
    Returns the number of candidates that remain.
    """
    candidates = load_results(engine, store_name, kind)
    if not candidates:
        return 0

    kept: list[AuditCandidate] = []
    changed = False
    for candidate in candidates:
        manifest = _load_manifest_rows(engine, store_name,
                                       candidate.store_app_id)
        by_downlink: dict[str, ArchiveEntry] = {}
        for e in manifest:
            path = _downlink_path((e.metadata or {}).get("downlink", ""))
            if not path:
                continue
            held = by_downlink.get(path)
            if held is None or _entry_release_key(e) > _entry_release_key(held):
                by_downlink[path] = e

        remaining = []
        for f in candidate.files:
            entry = by_downlink.get(_downlink_path(f.downlink))
            if entry is not None:
                satisfied, _reason = _compare({"version": f.version}, entry)
                if satisfied:
                    changed = True
                    continue
            remaining.append(f)

        # Retention hangs on the installers alone: once they are all
        # satisfied, leftover patch/extra rows must not keep the game
        # on the badge forever.
        if any(f.selected and f.kind == "installer" for f in remaining):
            if len(remaining) != len(candidate.files):
                candidate = AuditCandidate(
                    store_name=candidate.store_name,
                    store_app_id=candidate.store_app_id,
                    game_title=candidate.game_title,
                    kind=candidate.kind,
                    files=remaining,
                    total_bytes=sum(
                        f.size_bytes or 0 for f in remaining if f.selected),
                )
            kept.append(candidate)
        else:
            changed = True

    if changed:
        replace_results(engine, store_name, kind, kept)
    return len(kept)


def _insert_candidates(conn, candidates: list[AuditCandidate]) -> None:
    scanned_at = utc_now().isoformat()
    for candidate in candidates:
        conn.execute(
            text("""
                INSERT INTO download_audit_results (
                    id, store_name, kind, store_app_id, game_title,
                    payload_json, total_bytes, scanned_at
                ) VALUES (
                    :id, :store_name, :kind, :store_app_id, :game_title,
                    :payload_json, :total_bytes, :scanned_at
                )
            """),
            {
                "id": str(uuid.uuid4()),
                "store_name": candidate.store_name,
                "kind": candidate.kind,
                "store_app_id": candidate.store_app_id,
                "game_title": candidate.game_title,
                "payload_json": json.dumps(
                    [asdict(f) for f in candidate.files]),
                "total_bytes": candidate.total_bytes,
                "scanned_at": scanned_at,
            },
        )


def replace_results(
    engine: "Engine",
    store_name: str,
    kind: str,
    candidates: list[AuditCandidate],
) -> None:
    """Persist a scan's results, replacing the previous (store, kind) scan."""
    with engine.connect() as conn:
        conn.execute(
            text(
                "DELETE FROM download_audit_results "
                "WHERE store_name = :store AND kind = :kind"
            ),
            {"store": store_name, "kind": kind},
        )
        _insert_candidates(conn, candidates)
        conn.commit()


def merge_results(
    engine: "Engine",
    store_name: str,
    kind: str,
    scanned_app_ids: list[str],
    candidates: list[AuditCandidate],
) -> None:
    """Fold a partial (cancelled) scan into the persisted results.

    Rows for the games the scan reached are replaced by their fresh
    candidates (or removed when the game came back satisfied); games the
    scan never got to keep their previous result.
    """
    with engine.connect() as conn:
        for app_id in scanned_app_ids:
            conn.execute(
                text(
                    "DELETE FROM download_audit_results "
                    "WHERE store_name = :store AND kind = :kind "
                    "AND store_app_id = :app"
                ),
                {"store": store_name, "kind": kind, "app": app_id},
            )
        _insert_candidates(conn, candidates)
        conn.commit()


def load_results(
    engine: "Engine",
    store_name: str,
    kind: str,
) -> list[AuditCandidate]:
    """Load the last persisted scan for (store, kind).

    Build numbers are not part of the persisted payload; they are
    refilled from store_build_cache so the review dialog's Old/New
    columns render from the cache, never from a live lookup.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT store_app_id, game_title, payload_json, total_bytes "
                "FROM download_audit_results "
                "WHERE store_name = :store AND kind = :kind "
                "ORDER BY game_title"
            ),
            {"store": store_name, "kind": kind},
        ).fetchall()

    builds = load_builds(engine, store_name) if rows else {}
    candidates = []
    for app_id, game_title, payload_json, total_bytes in rows:
        files = [AuditFile(**d) for d in json.loads(payload_json)]
        local_build, online_build = builds.get(app_id, (None, None))
        candidates.append(AuditCandidate(
            store_name=store_name,
            store_app_id=app_id,
            game_title=game_title,
            kind=kind,
            files=files,
            total_bytes=total_bytes or 0,
            local_build=local_build,
            online_build=online_build,
        ))
    return candidates


def _current_preferences(config) -> tuple[list[str], list[str]]:
    """OS/language download preferences as the scan filter reads them."""
    if config is None:
        return [_detect_platform()], ["all"]
    preferred_os = config.get(
        "downloads.preferred_os", [_detect_platform()])
    preferred_languages = config.get(
        "downloads.preferred_languages", ["all"])
    return preferred_os, preferred_languages


def _preferences_fingerprint(
    preferred_os: list[str],
    preferred_languages: list[str],
) -> dict[str, list[str]]:
    # Normalized and sorted: the selection filter matches by membership
    # ("macos" folded to "mac", languages case-insensitive), so neither
    # order nor spelling variants may register as a settings change.
    return {
        "preferred_os": sorted(normalize_preferred_os(preferred_os)),
        "preferred_languages": sorted(
            lang.lower() for lang in (preferred_languages or [])),
    }


def record_scan_preferences(
    engine: "Engine",
    store_name: str,
    kind: str,
    preferred_os: list[str],
    preferred_languages: list[str],
) -> None:
    """Remember the preference set a completed scan was filtered with."""
    with engine.connect() as conn:
        conn.execute(
            text(
                "DELETE FROM download_audit_scans "
                "WHERE store_name = :store AND kind = :kind"
            ),
            {"store": store_name, "kind": kind},
        )
        conn.execute(
            text("""
                INSERT INTO download_audit_scans (
                    store_name, kind, scanned_at, preferences_json
                ) VALUES (:store_name, :kind, :scanned_at, :preferences_json)
            """),
            {
                "store_name": store_name,
                "kind": kind,
                "scanned_at": utc_now().isoformat(),
                "preferences_json": json.dumps(
                    _preferences_fingerprint(
                        preferred_os, preferred_languages)),
            },
        )
        conn.commit()


def load_scan_preferences(
    engine: "Engine",
    store_name: str,
    kind: str,
) -> Optional[dict]:
    """Preference fingerprint of the last completed scan, or None."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT preferences_json FROM download_audit_scans "
                "WHERE store_name = :store AND kind = :kind"
            ),
            {"store": store_name, "kind": kind},
        ).fetchone()
    if row is None:
        return None
    try:
        prefs = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    return prefs if isinstance(prefs, dict) else None


def scan_preferences_stale(
    engine: "Engine",
    store_name: str,
    kind: str,
    config,
) -> bool:
    """True when persisted results no longer match current preferences.

    OS-excluded files are not in the persisted payload, so results
    cannot be re-filtered at dialog time; all the UI can do is say so.
    Results scanned before fingerprints existed return False: there is
    no basis to judge them, and flagging every legacy result set would
    teach users to ignore the hint.
    """
    stored = load_scan_preferences(engine, store_name, kind)
    if stored is None:
        return False
    preferred_os, preferred_languages = _current_preferences(config)
    return stored != _preferences_fingerprint(
        preferred_os, preferred_languages)
