# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""Internal database migrations for luducat

This module provides an embedded migration system that works with compiled
binaries (Nuitka, PyInstaller). It uses SQLite PRAGMA user_version for
version tracking instead of Alembic's file-based approach.

Migration functions are defined in versions.py and compiled into the binary.
"""

import logging
from typing import Optional

from sqlalchemy import Engine, inspect, text

from .versions import (
    ALEMBIC_REVISION_MAP,
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
)

logger = logging.getLogger(__name__)


def get_schema_version(engine: Engine) -> int:
    """Get the current schema version from PRAGMA user_version.

    Args:
        engine: SQLAlchemy engine

    Returns:
        Current schema version (0 if not set)
    """
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA user_version"))
        row = result.fetchone()
        return row[0] if row else 0


def set_schema_version(engine: Engine, version: int) -> None:
    """Set the schema version in PRAGMA user_version.

    Args:
        engine: SQLAlchemy engine
        version: Version number to set
    """
    with engine.connect() as conn:
        # PRAGMA doesn't support parameters, must use string formatting
        # This is safe because version is always an int
        conn.execute(text(f"PRAGMA user_version = {int(version)}"))
        conn.commit()
    logger.debug(f"Set schema version to {version}")


def is_fresh_database(engine: Engine) -> bool:
    """Check if this is a fresh database with no tables.

    Args:
        engine: SQLAlchemy engine

    Returns:
        True if database has no tables
    """
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    return len(tables) == 0


def has_alembic_version(engine: Engine) -> Optional[str]:
    """Check if database has an alembic_version table and get current revision.

    Args:
        engine: SQLAlchemy engine

    Returns:
        Alembic revision string if table exists, None otherwise
    """
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    if "alembic_version" not in tables:
        return None

    with engine.connect() as conn:
        result = conn.execute(text("SELECT version_num FROM alembic_version"))
        row = result.fetchone()
        return row[0] if row else None


def migrate_from_alembic(engine: Engine) -> int:
    """Migrate from Alembic versioning to PRAGMA user_version.

    Reads the Alembic revision, maps it to an internal version,
    sets PRAGMA user_version, and drops the alembic_version table.

    Args:
        engine: SQLAlchemy engine

    Returns:
        The internal version number the database was migrated to
    """
    alembic_rev = has_alembic_version(engine)
    if not alembic_rev:
        return 0

    # Map Alembic revision to internal version
    version = ALEMBIC_REVISION_MAP.get(alembic_rev, CURRENT_SCHEMA_VERSION)
    logger.info(f"Migrating from Alembic revision '{alembic_rev}' to internal version {version}")

    # Set PRAGMA user_version
    set_schema_version(engine, version)

    # Drop alembic_version table
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
        conn.commit()
    logger.info("Dropped alembic_version table")

    return version


def needs_migration(engine: Engine) -> bool:
    """Check if database needs migration.

    Args:
        engine: SQLAlchemy engine

    Returns:
        True if migrations need to be applied
    """
    current = get_schema_version(engine)
    return current < CURRENT_SCHEMA_VERSION


def run_migrations(engine: Engine) -> None:
    """Run all pending migrations.

    Applies migrations sequentially from current version to CURRENT_SCHEMA_VERSION.

    Args:
        engine: SQLAlchemy engine
    """
    current = get_schema_version(engine)

    if current >= CURRENT_SCHEMA_VERSION:
        logger.debug(f"Database already at version {current}, no migrations needed")
        return

    logger.info(f"Running migrations from version {current} to {CURRENT_SCHEMA_VERSION}")

    with engine.connect() as conn:
        for from_ver, to_ver, migrate_fn in MIGRATIONS:
            if current == from_ver:
                logger.debug(f"Applying migration {from_ver} -> {to_ver}")
                migrate_fn(conn)
                current = to_ver

    # Set final version
    set_schema_version(engine, current)
    logger.info(f"Migrations complete, database at version {current}")


def verify_required_tables(engine: Engine) -> None:
    """Verify all required tables exist, create missing ones.

    This handles cases where:
    - Schema version is current but migrations failed silently
    - Tables weren't created due to bugs in earlier versions

    Tables not defined as ORM models (migration-only) are checked here.
    """
    # Tables that are only defined in migrations, not ORM models
    # Maps table name -> (migration_from, migration_to) for the migration that creates it
    MIGRATION_ONLY_TABLES = {
        "runtimes": (6, 7),
        "game_installations": (7, 8),
    }

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    missing = []
    for table_name, (from_ver, to_ver) in MIGRATION_ONLY_TABLES.items():
        if table_name not in existing_tables:
            missing.append((table_name, from_ver, to_ver))

    if not missing:
        return

    logger.warning(f"Missing tables detected: {[m[0] for m in missing]}")

    # Run the specific migrations for missing tables
    with engine.connect() as conn:
        for table_name, from_ver, to_ver in missing:
            # Find the migration function
            for mig_from, mig_to, migrate_fn in MIGRATIONS:
                if mig_from == from_ver and mig_to == to_ver:
                    logger.info(f"Creating missing table '{table_name}' via migration {from_ver}->{to_ver}")
                    try:
                        migrate_fn(conn)
                    except Exception as e:
                        logger.error(f"Failed to create table '{table_name}': {e}")
                    break


def init_or_migrate(engine: Engine) -> None:
    """Initialize or migrate database as needed.

    This is the main entry point for database setup:
    - Fresh database: Create tables via SQLAlchemy, set version to current
    - Existing Alembic database: Convert to PRAGMA versioning, then migrate
    - Existing PRAGMA database: Run pending migrations
    - Always: Verify required tables exist (repair if needed)

    Args:
        engine: SQLAlchemy engine
    """
    from luducat.core.database import Base

    # Check for Alembic-managed database first
    alembic_rev = has_alembic_version(engine)
    if alembic_rev:
        logger.info(f"Detected Alembic-managed database at revision '{alembic_rev}'")
        migrate_from_alembic(engine)

    current = get_schema_version(engine)

    if current == 0:
        # Version 0 could be fresh database or pre-versioned database
        if is_fresh_database(engine):
            # Fresh database - create tables via ORM and migrations
            logger.info("Fresh database - creating tables")
            Base.metadata.create_all(engine)
            # Also run all migrations to create tables not defined as ORM models
            # (archives, runtimes, game_installations are migration-only)
            with engine.connect() as conn:
                for from_ver, to_ver, migrate_fn in MIGRATIONS:
                    try:
                        migrate_fn(conn)
                    except Exception as e:
                        # Tables may already exist from create_all, that's OK
                        logger.debug(f"Migration {from_ver}->{to_ver} skipped: {e}")
            set_schema_version(engine, CURRENT_SCHEMA_VERSION)
            logger.info(f"Database initialized at version {CURRENT_SCHEMA_VERSION}")
        else:
            # Existing database without version tracking
            # Assume it's at version 1 (initial schema) and migrate from there
            logger.info("Pre-versioned database detected, assuming version 1")
            set_schema_version(engine, 1)
            if needs_migration(engine):
                run_migrations(engine)
    elif current < CURRENT_SCHEMA_VERSION:
        # Database needs migration
        logger.info(f"Database at version {current}, migrating to {CURRENT_SCHEMA_VERSION}")
        run_migrations(engine)
    else:
        logger.debug(f"Database is up to date at version {current}")

    # Always verify required tables exist (repairs databases with missing tables)
    verify_required_tables(engine)


# Compatibility exports for any code that imported these
__all__ = [
    "get_schema_version",
    "set_schema_version",
    "is_fresh_database",
    "needs_migration",
    "run_migrations",
    "init_or_migrate",
    "verify_required_tables",
    "CURRENT_SCHEMA_VERSION",
]
