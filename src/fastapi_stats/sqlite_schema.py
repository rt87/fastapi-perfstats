"""SQLite schema and its version (owned by #2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    key TEXT PRIMARY KEY, count INTEGER NOT NULL, e4 INTEGER NOT NULL, e5 INTEGER NOT NULL,
    total_ms REAL NOT NULL, min_ms REAL NOT NULL, max_ms REAL NOT NULL,
    first_ts REAL NOT NULL, last_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS durations (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, ms REAL NOT NULL);
CREATE INDEX IF NOT EXISTS durations_key ON durations (key, id);
CREATE TABLE IF NOT EXISTS slow_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, key TEXT NOT NULL,
    ms REAL NOT NULL, status INTEGER NOT NULL);
"""


def ensure_schema(path: str | Path) -> None:
    """Create the tables in the database file at ``path`` (own connection, closed afterwards)."""
    db = sqlite3.connect(path)
    try:
        db.executescript(SCHEMA)
        db.commit()
    finally:
        db.close()
