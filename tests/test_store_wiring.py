"""Store wiring: extension hooks, slow-call extras, reset, dropped and close, for both stores."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from fakes import FIXED_TS, make_call
from fastapi_stats.config import StatsConfig
from fastapi_stats.ext import EXTENSIONS
from fastapi_stats.ext.base import NoopExtension
from fastapi_stats.models import OTHER_KEY, CallRecord, RouteStats
from fastapi_stats.stores import MemoryStore, SqliteStore, StatsStore


class RecordingExt(NoopExtension):
    """Records every hook call; keeps a counter table in SQLite to prove reset / rollback behaviour."""

    def __init__(self, name: str = "rec") -> None:
        super().__init__(name)
        self.calls: list[tuple[Any, ...]] = []
        self.fail_on_update = False

    def sqlite_schema(self) -> tuple[str, ...]:
        return ("CREATE TABLE IF NOT EXISTS rec_hits (key TEXT NOT NULL)",)

    def memory_update(self, state: dict[str, Any], call: CallRecord) -> None:
        state["n"] = state.get("n", 0) + 1
        self.calls.append(("memory_update", call.key))

    def memory_finish(self, row: RouteStats, state: dict[str, Any], config: StatsConfig, now: float) -> None:
        row.stddev_ms = float(state["n"])
        self.calls.append(("memory_finish", row.key, now))

    def sqlite_update(self, db: sqlite3.Connection, call: CallRecord) -> None:
        if self.fail_on_update:
            raise RuntimeError("boom")
        db.execute("INSERT INTO rec_hits (key) VALUES (?)", (call.key,))
        self.calls.append(("sqlite_update", call.key))

    def sqlite_prune(self, db: sqlite3.Connection, config: StatsConfig, now: float) -> None:
        self.calls.append(("sqlite_prune", config.window_size))

    def sqlite_finish(
        self, rows: Mapping[str, RouteStats], db: sqlite3.Connection, config: StatsConfig, now: float
    ) -> None:
        for key, row in rows.items():
            row.stddev_ms = float(db.execute("SELECT COUNT(*) FROM rec_hits WHERE key = ?", (key,)).fetchone()[0])
        self.calls.append(("sqlite_finish", tuple(sorted(rows))))

    def sqlite_reset(self, db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM rec_hits")
        self.calls.append(("sqlite_reset",))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


StoreFactory = Callable[..., StatsStore]


@pytest.fixture(params=["memory", "sqlite"])
def make_store(request: pytest.FixtureRequest, tmp_path: Path) -> StoreFactory:
    def build(extensions: list[Any] | None = None, **cfg: Any) -> StatsStore:
        config = StatsConfig(sqlite_path=str(tmp_path / "w.db"), **cfg)
        store: StatsStore = (
            MemoryStore(config, extensions) if request.param == "memory" else SqliteStore(config, extensions)
        )
        request.addfinalizer(store.close)
        return store

    return build


# ---- hooks -----------------------------------------------------------------------------------------------
def test_hooks_called_with_final_key_including_other_bucket(make_store: StoreFactory) -> None:
    ext = RecordingExt()
    store = make_store([ext], max_routes=1)
    store.record(make_call("a"))
    store.record(make_call("b"))  # over the cap -> "other"
    store.record(make_call("c"))
    rows = {r.key: r for r in store.routes()}
    assert set(rows) == {"a", OTHER_KEY}
    updates = [c[1] for c in ext.calls if c[0].endswith("_update")]
    assert updates == ["a", OTHER_KEY, OTHER_KEY]
    # finish hook ran in routes() and could modify the rows
    assert rows[OTHER_KEY].stddev_ms == 2.0 and rows["a"].stddev_ms == 1.0


def test_default_extensions_are_the_registry(tmp_path: Path) -> None:
    assert MemoryStore(StatsConfig())._exts == EXTENSIONS
    store = SqliteStore(StatsConfig(sqlite_path=str(tmp_path / "d.db")))
    try:
        assert store._exts == EXTENSIONS
    finally:
        store.close()


def test_empty_extension_list_means_no_hooks(make_store: StoreFactory) -> None:
    store = make_store([])
    store.record(make_call())
    assert store.routes()[0].stddev_ms is None


def test_memory_hook_order_and_per_route_state() -> None:
    ext = RecordingExt()
    store = MemoryStore(StatsConfig(), [ext])
    store.record(make_call("a"))
    store.record(make_call("b"))
    store.record(make_call("a"))
    store.routes()
    assert ext.names() == ["memory_update"] * 3 + ["memory_finish"] * 2


def test_sqlite_schema_prune_and_finish_hooks(tmp_path: Path) -> None:
    ext = RecordingExt()
    path = tmp_path / "h.db"
    store = SqliteStore(StatsConfig(sqlite_path=str(path), window_size=7), [ext])
    try:
        assert ext.calls == []  # schema statements are executed, not hook-called per record
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT COUNT(*) FROM rec_hits").fetchone() == (0,)
        for _ in range(3):
            store.record(make_call("a"))
        store.routes()
        assert ext.names().count("sqlite_update") == 3
        assert "sqlite_prune" in ext.names() and ("sqlite_prune", 7) in ext.calls
        assert ext.calls[-1] == ("sqlite_finish", ("a",))
    finally:
        store.close()


def test_sqlite_prune_runs_once_per_batch_not_per_record(tmp_path: Path) -> None:
    ext = RecordingExt()
    store = SqliteStore(StatsConfig(sqlite_path=str(tmp_path / "b.db")), [ext])
    try:
        store.flush()
        store._apply([make_call("a"), make_call("a"), make_call("b")])
        assert ext.names().count("sqlite_prune") == 1
        store._apply([])  # empty batch: no hooks at all
        assert ext.names().count("sqlite_prune") == 1
    finally:
        store.close()


def test_sqlite_state_survives_restart_and_schema_is_idempotent(tmp_path: Path) -> None:
    path = str(tmp_path / "r.db")
    first = SqliteStore(StatsConfig(sqlite_path=path), [RecordingExt()])
    first.record(make_call("a"))
    first.flush()
    first.close()
    second = SqliteStore(StatsConfig(sqlite_path=path), [RecordingExt()])
    try:
        assert second.routes()[0].stddev_ms == 1.0
    finally:
        second.close()


# ---- slow calls ------------------------------------------------------------------------------------------
def test_slow_call_extras_kept(make_store: StoreFactory) -> None:
    store = make_store([], slow_threshold_ms=100)
    store.record(
        make_call("a", 500.0, query="q=1", request_id="req-1", context={"user": "u1", "tenant": "t1"}, ts=FIXED_TS)
    )
    store.record(make_call("a", 600.0))  # slow, but no extras
    store.record(make_call("a", 5.0, query="fast"))  # not slow: nothing stored
    newest, oldest = store.slow_calls()
    assert (oldest.query, oldest.request_id, oldest.context) == ("q=1", "req-1", {"user": "u1", "tenant": "t1"})
    assert (newest.query, newest.request_id, newest.context) == (None, None, {})
    assert oldest.key == "a" and oldest.duration_ms == 500.0 and oldest.status == 200 and oldest.ts == FIXED_TS


def test_slow_call_extras_pruned_with_slow_log(tmp_path: Path) -> None:
    store = SqliteStore(StatsConfig(sqlite_path=str(tmp_path / "s.db"), slow_threshold_ms=1, max_slow_calls=3), [])
    try:
        for i in range(10):
            store.record(make_call("k", 10.0 + i, request_id=f"r{i}"))
        assert [c.request_id for c in store.slow_calls()] == ["r9", "r8", "r7"]
        assert store.row_counts()["slow_call_extra"] == 3
    finally:
        store.close()


def test_slow_extras_persist_as_json_and_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "j.db"
    cfg = StatsConfig(sqlite_path=str(path), slow_threshold_ms=0)
    store = SqliteStore(cfg, [])
    store.record(make_call("a", context={"k": "v"}))
    store.flush()
    with sqlite3.connect(path) as db:
        (raw,) = db.execute("SELECT context FROM slow_call_extra").fetchone()
    assert json.loads(raw) == {"k": "v"}
    store.close()
    again = SqliteStore(cfg, [])
    try:
        assert again.slow_calls()[0].context == {"k": "v"}
    finally:
        again.close()


def test_slow_log_uses_per_key_threshold(make_store: StoreFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        StatsConfig, "slow_threshold_for", lambda self, key: {"fast": 5.0, "never": None}.get(key, 1000.0)
    )
    store = make_store([])
    for key in ("fast", "never", "normal"):
        store.record(make_call(key, 50.0))
    assert [c.key for c in store.slow_calls()] == ["fast"]


def test_slow_threshold_applies_to_final_other_key(make_store: StoreFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def threshold(self: StatsConfig, key: str) -> float | None:
        seen.append(key)
        return 1.0

    monkeypatch.setattr(StatsConfig, "slow_threshold_for", threshold)
    store = make_store([], max_routes=1)
    store.record(make_call("a", 5.0))
    store.record(make_call("b", 5.0))
    store.flush()
    assert seen == ["a", OTHER_KEY]


# ---- reset -----------------------------------------------------------------------------------------------
def test_reset_clears_everything_and_extensions(make_store: StoreFactory) -> None:
    ext = RecordingExt()
    store = make_store([ext], slow_threshold_ms=0)
    store.record(make_call("a", request_id="r"))
    assert store.routes() and store.slow_calls()
    store.reset()
    assert store.routes() == [] and store.slow_calls() == []
    store.record(make_call("a"))
    assert store.routes()[0].count == 1
    assert store.routes()[0].stddev_ms == 1.0  # extension state restarted from zero


def test_sqlite_reset_empties_every_table_and_forgets_known_keys(tmp_path: Path) -> None:
    ext = RecordingExt()
    store = SqliteStore(StatsConfig(sqlite_path=str(tmp_path / "x.db"), slow_threshold_ms=0, max_routes=1), [ext])
    try:
        store.record(make_call("a"))
        store.record(make_call("b"))  # -> other
        store.flush()
        store.reset()
        assert store.row_counts() == {"routes": 0, "durations": 0, "slow_calls": 0, "slow_call_extra": 0}
        assert ("sqlite_reset",) in ext.calls
        store.record(make_call("c"))  # the route cap starts fresh: "c" is a real key again
        assert [r.key for r in store.routes()] == ["c"]
    finally:
        store.close()


def test_sqlite_reset_is_ordered_with_the_queue(tmp_path: Path) -> None:
    store = SqliteStore(StatsConfig(sqlite_path=str(tmp_path / "o.db")), [])
    try:
        for _ in range(50):
            store.record(make_call("before"))
            while store._q.qsize() > 0:  # the writer took it and now waits for the lock
                threading.Event().wait(0.01)
        store.reset()
        for _ in range(3):
            store.record(make_call("after"))
        assert {r.key: r.count for r in store.routes()} == {"after": 3}
    finally:
        store.close()


def test_sqlite_reset_when_records_and_reset_share_one_batch(tmp_path: Path) -> None:
    store = SqliteStore(StatsConfig(sqlite_path=str(tmp_path / "q.db")), [])
    try:
        gate = store._lock
        gate.acquire()  # hold the writer back so everything below lands in a single batch
        try:
            store.record(make_call("before"))
            while store._q.qsize() > 0:  # the writer took it and now waits for the lock
                threading.Event().wait(0.01)
            marker_done: list[Exception | None] = []
            thread = threading.Thread(target=lambda: marker_done.append(_try(store.reset)))
            thread.start()
            while store._q.qsize() < 1:  # the reset marker is queued
                threading.Event().wait(0.01)
            store.record(make_call("after"))
        finally:
            gate.release()
        thread.join(timeout=10)
        assert marker_done == [None]
        assert {r.key: r.count for r in store.routes()} == {"after": 1}
    finally:
        store.close()


def _try(fn: Callable[[], None]) -> Exception | None:
    try:
        fn()
    except Exception as exc:
        return exc
    return None


def test_sqlite_reset_on_closed_store_raises_clear_error(tmp_path: Path) -> None:
    store = SqliteStore(StatsConfig(sqlite_path=str(tmp_path / "c.db")), [])
    store.close()
    with pytest.raises(RuntimeError, match="closed"):
        store.reset()


def test_sqlite_reset_failure_is_raised_and_store_keeps_running(tmp_path: Path) -> None:
    store = SqliteStore(StatsConfig(sqlite_path=str(tmp_path / "f.db")), [])
    try:
        store.record(make_call("a"))
        store.flush()
        store._db.execute("ALTER TABLE slow_call_extra RENAME TO gone")
        with pytest.raises(sqlite3.Error):
            store.reset()
        store._db.execute("ALTER TABLE gone RENAME TO slow_call_extra")
        assert store._thread.is_alive()
        assert store.routes()[0].count == 1  # failed reset rolled back, nothing lost
    finally:
        store.close()


# ---- failures inside the writer --------------------------------------------------------------------------
def test_extension_error_rolls_back_batch_and_store_keeps_running(tmp_path: Path) -> None:
    ext = RecordingExt()
    store = SqliteStore(StatsConfig(sqlite_path=str(tmp_path / "e.db"), max_routes=2), [ext])
    try:
        store.record(make_call("keep"))
        store.flush()
        ext.fail_on_update = True
        store.record(make_call("lost-1"))
        store.record(make_call("lost-2"))
        store.flush()
        assert [r.key for r in store.routes()] == ["keep"]  # the failed batch left no trace
        ext.fail_on_update = False
        store.record(make_call("lost-1"))  # not stuck in _known: it is still a real key
        assert {r.key for r in store.routes()} == {"keep", "lost-1"}
    finally:
        store.close()


def test_extension_error_does_not_break_other_batches(tmp_path: Path) -> None:
    ext = RecordingExt()
    store = SqliteStore(StatsConfig(sqlite_path=str(tmp_path / "e2.db")), [ext])
    try:
        ext.fail_on_update = True
        store.record(make_call("x"))
        store.flush()
        assert store._thread.is_alive()
        assert store.routes() == []
    finally:
        store.close()


# ---- dropped and close -----------------------------------------------------------------------------------
def test_dropped_counts_queue_full_drops(tmp_path: Path) -> None:
    store = SqliteStore(StatsConfig(sqlite_path=str(tmp_path / "d.db")), [])
    try:
        store.flush()
        full: queue.Queue[object] = queue.Queue(1)
        full.put("filler")
        real_q, store._q = store._q, full
        for _ in range(3):
            store.record(make_call())
        assert store.dropped == 3
        store._q = real_q
        store.record(make_call())
        assert store.dropped == 3
    finally:
        store.close()


def test_memory_store_never_drops() -> None:
    store = MemoryStore(StatsConfig(), [])
    store.record(make_call())
    assert store.dropped == 0


def test_close_twice_raises_nothing(make_store: StoreFactory) -> None:
    store = make_store([])
    store.record(make_call())
    store.close()
    store.close()
    store.flush()  # reads/flush after close stay harmless


def test_memory_reset_does_not_need_close() -> None:
    store = MemoryStore(StatsConfig(), [])
    store.reset()
    assert store.routes() == []
