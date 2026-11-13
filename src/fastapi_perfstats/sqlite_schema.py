"""SQLite schema and its version (owned by #2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_STATEMENTS = (
    "CREATE TABLE routes ("
    "key TEXT PRIMARY KEY, count INTEGER NOT NULL, e4 INTEGER NOT NULL, e5 INTEGER NOT NULL, "
    "total_ms REAL NOT NULL, min_ms REAL NOT NULL, max_ms REAL NOT NULL, "
    "first_ts REAL NOT NULL, last_ts REAL NOT NULL)",
    "CREATE TABLE durations (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, ms REAL NOT NULL)",
    "CREATE INDEX durations_key ON durations (key, id)",
    "CREATE TABLE slow_calls ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, key TEXT NOT NULL, "
    "ms REAL NOT NULL, status INTEGER NOT NULL)",
)

_BUSY_TIMEOUT_S = 30.0


def ensure_schema(path: str | Path) -> None:
    """Create the core tables in a new/empty file and stamp it; accept ``SCHEMA_VERSION``; refuse anything else.

    Uses its own short-lived connection. ``BEGIN IMMEDIATE`` serialises concurrent workers. Raises ``RuntimeError``
    for a file with another version or with tables but no version stamp; never migrates or deletes anything.
    """
    db = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_S, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            found = int(db.execute("PRAGMA user_version").fetchone()[0])
            has_tables = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone() is not None
            if found == SCHEMA_VERSION:
                pass
            elif found == 0 and not has_tables:
                for statement in _STATEMENTS:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                raise RuntimeError(
                    f"stats database {str(path)!r} has schema version {found}, expected {SCHEMA_VERSION}; "
                    "delete the file or point sqlite_path elsewhere"
                )
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    finally:
        db.close()
