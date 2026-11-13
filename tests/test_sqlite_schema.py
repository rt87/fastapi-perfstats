from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from fastapi_perfstats.sqlite_schema import SCHEMA_VERSION, ensure_schema

CORE_TABLES = {"routes", "durations", "slow_calls"}


def _tables(path: Path) -> set[str]:
    db = sqlite3.connect(path)
    try:
        rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        return {row[0] for row in rows}
    finally:
        db.close()


def _user_version(path: Path) -> int:
    db = sqlite3.connect(path)
    try:
        return int(db.execute("PRAGMA user_version").fetchone()[0])
    finally:
        db.close()


def test_new_file_gets_tables_and_version(tmp_path: Path) -> None:
    path = tmp_path / "stats.db"
    ensure_schema(str(path))
    assert _tables(path) >= CORE_TABLES
    assert _user_version(path) == SCHEMA_VERSION == 1


def test_path_object_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "stats.db"
    ensure_schema(path)
    assert _user_version(path) == SCHEMA_VERSION


def test_second_open_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "stats.db"
    ensure_schema(str(path))
    db = sqlite3.connect(path)
    db.execute("INSERT INTO slow_calls (ts, key, ms, status) VALUES (1.0, 'GET /a', 700.0, 200)")
    db.commit()
    db.close()
    ensure_schema(str(path))
    db = sqlite3.connect(path)
    try:
        assert db.execute("SELECT key, ms FROM slow_calls").fetchall() == [("GET /a", 700.0)]
    finally:
        db.close()
    assert _user_version(path) == SCHEMA_VERSION


def test_extension_tables_do_not_disturb_a_stamped_file(tmp_path: Path) -> None:
    path = tmp_path / "stats.db"
    ensure_schema(str(path))
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE ext_demo (id INTEGER)")
    db.commit()
    db.close()
    ensure_schema(str(path))
    assert "ext_demo" in _tables(path)


def test_unversioned_file_with_tables_is_refused_and_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE routes (key TEXT)")
    db.commit()
    db.close()
    before = path.read_bytes()
    with pytest.raises(RuntimeError) as info:
        ensure_schema(str(path))
    message = str(info.value)
    assert "old.db" in message
    assert "schema version 0" in message
    assert f"expected {SCHEMA_VERSION}" in message
    assert path.read_bytes() == before
    assert _tables(path) == {"routes"}


def test_other_version_is_refused_and_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.close()
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="schema version 99, expected 1"):
        ensure_schema(str(path))
    assert path.read_bytes() == before
    assert _tables(path) == set()
    assert _user_version(path) == 99


def test_non_sqlite_file_raises_database_error(tmp_path: Path) -> None:
    path = tmp_path / "junk.db"
    path.write_bytes(os.urandom(4096))
    before = path.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        ensure_schema(str(path))
    assert path.read_bytes() == before


def test_four_threads_opening_one_new_file_all_succeed(tmp_path: Path) -> None:
    path = tmp_path / "shared.db"
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        try:
            barrier.wait()
            ensure_schema(str(path))
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert _tables(path) >= CORE_TABLES
    assert _user_version(path) == SCHEMA_VERSION
