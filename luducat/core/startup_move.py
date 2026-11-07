# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# startup_move.py

"""Data/cache directory moves, recorded in Settings and run at the next start.

A live copy loses writes to open databases (lud-sdvi), so the copy runs before
anything opens the files.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import config as cfg
from .config import Config, DirLayout, LayoutError

logger = logging.getLogger(__name__)

# kind -> (override key, pending key)
_KEYS = {
    "data": ("app.custom_data_dir", "app.pending_move_data"),
    "cache": ("app.custom_cache_dir", "app.pending_move_cache"),
}

CopyFn = Callable[[str, Path, Path], list[Path]]


class MoveError(RuntimeError):
    """A directory move is not allowed or did not happen."""


@dataclass(frozen=True)
class PendingMove:
    kind: str
    source: Path
    target: Path
    delete_source: bool


@dataclass(frozen=True)
class MoveOutcome:
    kind: str
    target: Path | None
    error: str | None


def _keys(kind: str) -> tuple[str, str]:
    if kind not in _KEYS:
        raise ValueError(f"Unknown directory kind: {kind!r}")
    return _KEYS[kind]


def current_dir(config: Config, kind: str) -> Path:
    """The directory of this kind in effect for the given config."""
    override_key, _pending = _keys(kind)
    custom = config.get(override_key, "")
    return Path(custom) if custom else getattr(cfg.platform_layout(), kind)


def _planned_dir(config: Config, kind: str) -> Path:
    """Where this kind will live after the moves already recorded."""
    try:
        move = pending_move(config, kind)
    except MoveError:
        move = None
    return move.target if move else current_dir(config, kind)


def validate_move(config: Config, kind: str, target: Path) -> Path:
    """Check a move of the current `kind` directory to target; return the source."""
    source = current_dir(config, kind)
    target = Path(target)
    if not target.is_absolute():
        raise MoveError(f"The new location must be an absolute path: {target}")
    if not source.is_dir():
        raise MoveError(f"The current folder does not exist: {source}")

    ci = cfg.is_case_insensitive_platform()
    if cfg.path_contains(source, target, ci):
        raise MoveError(f"The new location {target} is inside the current folder {source}.")
    if cfg.path_contains(target, source, ci):
        raise MoveError(f"The current folder {source} is inside the new location {target}.")

    layout = DirLayout(
        config=cfg.get_config_dir(),
        data=target if kind == "data" else _planned_dir(config, "data"),
        cache=target if kind == "cache" else _planned_dir(config, "cache"),
    )
    try:
        cfg.check_layout(layout, case_insensitive=ci)
    except LayoutError as e:
        raise MoveError(str(e)) from e

    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise MoveError(f"The new location must be an empty folder: {target}")
    return source


def schedule_move(config: Config, kind: str, target: Path, delete_source: bool) -> PendingMove:
    """Validate and record a move to run at the next start."""
    source = validate_move(config, kind, target)
    move = PendingMove(kind, source, Path(target), delete_source)
    _override, pending_key = _keys(kind)
    config.set(pending_key, {
        "source": str(move.source),
        "target": str(move.target),
        "delete_source": move.delete_source,
    })
    config.save()
    logger.info("Scheduled %s move %s -> %s", kind, move.source, move.target)
    return move


def cancel_move(config: Config, kind: str) -> None:
    _override, pending_key = _keys(kind)
    config.set(pending_key, None)
    config.save()


def pending_move(config: Config, kind: str) -> PendingMove | None:
    """The recorded move of this kind, None if none.

    Raises:
        MoveError: the recorded entry is malformed.
    """
    _override, pending_key = _keys(kind)
    entry = config.get(pending_key)
    if entry is None:
        return None
    if not (
        isinstance(entry, dict)
        and isinstance(entry.get("source"), str)
        and isinstance(entry.get("target"), str)
        and isinstance(entry.get("delete_source"), bool)
    ):
        raise MoveError(f"Malformed {pending_key} entry in the config: {entry!r}")
    return PendingMove(kind, Path(entry["source"]), Path(entry["target"]),
                       entry["delete_source"])


def run_pending_moves(config: Config, copy_fn: CopyFn) -> list[MoveOutcome]:
    """Execute recorded moves and clear each one from the config, whatever the result.

    copy_fn raises DirectoryMoveError after its own rollback.
    """
    from .directory_migrate import DirectoryMoveError

    outcomes = []
    for kind in _KEYS:
        override_key, pending_key = _keys(kind)
        try:
            move = pending_move(config, kind)
        except MoveError as e:
            config.set(pending_key, None)
            config.save()
            outcomes.append(MoveOutcome(kind, None, str(e)))
            continue
        if move is None:
            continue

        try:
            source = validate_move(config, kind, move.target)
            if source != move.source:
                raise MoveError(
                    f"The {kind} folder is no longer {move.source} (now {source}); "
                    "the move was not done."
                )
            logger.info("Moving %s folder %s -> %s", kind, source, move.target)
            files = copy_fn(kind, source, move.target)
        except Exception as e:
            # Boundary: a recorded move must never fail the same way on every start.
            if not isinstance(e, (MoveError, DirectoryMoveError)):
                logger.exception("%s move to %s failed unexpectedly", kind, move.target)
            else:
                logger.error("%s move to %s failed: %s", kind, move.target, e)
            config.set(pending_key, None)
            config.save()
            outcomes.append(MoveOutcome(kind, move.target, str(e)))
            continue

        config.set(override_key, str(move.target))
        config.set(pending_key, None)
        config.save()
        logger.info("Moved %s folder %s -> %s (%d files)", kind, source, move.target, len(files))

        if move.delete_source:
            left = remove_moved_source(source, files)
            if left:
                logger.warning("Old %s folder %s kept: %d entries were not part of the move",
                               kind, source, left)
        outcomes.append(MoveOutcome(kind, move.target, None))
    return outcomes


def remove_moved_source(source: Path, files: list[Path]) -> int:
    """Never removes anything that was not copied; returns how many entries were left behind."""
    for rel in files:
        try:
            (source / rel).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not remove %s: %s", source / rel, e)
    for d in sorted((d for d in source.rglob("*") if d.is_dir()),
                    key=lambda d: len(d.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            logger.debug("Kept %s: holds entries that were not moved", d)
    left = 0
    try:
        left = sum(1 for _entry in source.rglob("*"))
        if left == 0:
            source.rmdir()
    except OSError as e:
        # The move already happened; a leftover old folder must not fail startup.
        logger.warning("Could not finish removing %s: %s", source, e)
    return left
