"""Storage backends: in-memory (default) and SQLite (stdlib), behind one ABC."""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from fastapi_perfstats import sqlite_schema
from fastapi_perfstats.config import StatsConfig, quantile_label
from fastapi_perfstats.ext.base import Extension
from fastapi_perfstats.models import OTHER_KEY, CallRecord, RouteStats, SlowCall, nearest_rank

log = logging.getLogger(__name__)


def _resolve_extensions(extensions: Sequence[Extension] | None) -> tuple[Extension, ...]:
    """``None`` means the registered extensions (imported lazily: the registry imports modules that use stores)."""
    if extensions is None:
        from fastapi_perfstats.ext import EXTENSIONS

        return EXTENSIONS
    return tuple(extensions)


class StatsStore(ABC):
    """Minimal contract. ``record`` must be cheap and thread-safe; reads may be slower."""

    @abstractmethod
    def record(self, call: CallRecord) -> None: ...

    @abstractmethod
    def routes(self) -> list[RouteStats]: ...

    @abstractmethod
    def slow_calls(self) -> list[SlowCall]: ...

    def reset(self) -> None:
        """Forget all statistics. Optional; stores that support it override this."""
        raise NotImplementedError(f"{type(self).__name__} does not support reset()")

    @property
    def dropped(self) -> int:
        """Records dropped because the store could not keep up (0 for stores that never drop)."""
        return 0

    def flush(self) -> None:  # noqa: B027 - optional hook
        """Block until everything recorded so far is visible to readers."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release resources."""


def _finish(rows: list[RouteStats], now: float) -> list[RouteStats]:
    total = sum(r.count for r in rows) or 1
    for r in rows:
        r.share = r.count / total
        r.rate_per_min = r.count / max(now - r.first_ts, 1.0) * 60
    return sorted(rows, key=lambda r: r.count, reverse=True)


@dataclass
class _Agg:
    window: deque[float]
    count: int = 0
    e4: int = 0
    e5: int = 0
    total: float = 0.0
    lo: float = float("inf")
    hi: float = 0.0
    first: float = 0.0
    last: float = 0.0
    ext: dict[str, dict[str, Any]] = field(default_factory=dict)  # per-extension state, keyed by extension name


def _make_stats(cfg: StatsConfig, key: str, agg: _Agg) -> RouteStats:
    window = sorted(agg.window)
    return RouteStats(
        key=key,
        count=agg.count,
        errors_4xx=agg.e4,
        errors_5xx=agg.e5,
        min_ms=agg.lo,
        mean_ms=agg.total / agg.count if agg.count else 0.0,
        max_ms=agg.hi,
        quantiles={quantile_label(q): nearest_rank(window, q) for q in cfg.quantiles},
        first_ts=agg.first,
        last_ts=agg.last,
    )


class MemoryStore(StatsStore):
    """Bounded in-memory store: ring buffer of ``window_size`` durations per route, ``max_routes`` routes."""

    def __init__(self, config: StatsConfig, extensions: Sequence[Extension] | None = None) -> None:
        self.cfg = config
        self._exts = _resolve_extensions(extensions)
        self._lock = threading.Lock()
        self._aggs: dict[str, _Agg] = {}
        self._slow: deque[SlowCall] = deque(maxlen=config.max_slow_calls)

    def record(self, call: CallRecord) -> None:
        with self._lock:
            if call.key not in self._aggs and len(self._aggs) >= self.cfg.max_routes and call.key != OTHER_KEY:
                call = replace(call, key=OTHER_KEY)
            key, duration_ms, status, ts = call.key, call.duration_ms, call.status, call.ts
            agg = self._aggs.get(key)
            if agg is None:
                agg = self._aggs[key] = _Agg(
                    window=deque(maxlen=self.cfg.window_size), first=ts, ext={e.name: {} for e in self._exts}
                )
            agg.count += 1
            agg.e5 += status >= 500
            agg.e4 += 400 <= status < 500
            agg.total += duration_ms
            agg.lo = min(agg.lo, duration_ms)
            agg.hi = max(agg.hi, duration_ms)
            agg.last = ts
            agg.window.append(duration_ms)
            for ext in self._exts:
                ext.memory_update(agg.ext[ext.name], call)
            thr = self.cfg.slow_threshold_for(key)
            if thr is not None and duration_ms >= thr:
                self._slow.append(
                    SlowCall(
                        ts,
                        key,
                        duration_ms,
                        status,
                        query=call.query,
                        request_id=call.request_id,
                        context=dict(call.context or {}),
                    )
                )

    def routes(self) -> list[RouteStats]:
        now = time.time()
        with self._lock:
            rows = []
            for k, a in self._aggs.items():
                row = _make_stats(self.cfg, k, a)
                for ext in self._exts:
                    ext.memory_finish(row, a.ext[ext.name], self.cfg, now)
                rows.append(row)
        return _finish(rows, now)

    def slow_calls(self) -> list[SlowCall]:
        with self._lock:
            return list(reversed(self._slow))  # newest first

    def reset(self) -> None:
        with self._lock:
            self._aggs.clear()
            self._slow.clear()


_STOP = object()


class _Reset:
    """Queue sentinel: the writer clears everything when it reaches it, in order with the records."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: Exception | None = None


_UPSERT = """
INSERT INTO routes (key, count, e4, e5, total_ms, min_ms, max_ms, first_ts, last_ts)
VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(key) DO UPDATE SET count = count + 1, e4 = e4 + excluded.e4, e5 = e5 + excluded.e5,
    total_ms = total_ms + excluded.total_ms, min_ms = MIN(min_ms, excluded.min_ms),
    max_ms = MAX(max_ms, excluded.max_ms), last_ts = excluded.last_ts
"""
_PRUNE = (
    "DELETE FROM durations WHERE key = ? AND id NOT IN "
    "(SELECT id FROM durations WHERE key = ? ORDER BY id DESC LIMIT ?)"
)
_SLOW_EXTRA_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS slow_call_extra "
    "(slow_id INTEGER PRIMARY KEY, query TEXT, request_id TEXT, context TEXT)"
)
_RESET_TABLES = ("routes", "durations", "slow_calls", "slow_call_extra")


class SqliteStore(StatsStore):
    """Persistent store. ``record`` only enqueues; one writer thread applies batches in one transaction.

    The queue is bounded (``QUEUE_SIZE``); if the writer cannot keep up, new records are dropped
    (counted in ``dropped``) instead of blocking the event loop or growing memory without limit.
    Size limits: ``window_size`` durations per route, ``max_routes`` routes (rest -> "other"),
    ``max_slow_calls`` slow-call rows. Reads call ``flush()`` first, so they see all prior records.
    A batch whose SQL or extension hook fails is rolled back and logged; the writer keeps running.
    """

    QUEUE_SIZE = 10_000
    BATCH = 500

    def __init__(self, config: StatsConfig, extensions: Sequence[Extension] | None = None) -> None:
        self.cfg = config
        self._exts = _resolve_extensions(extensions)
        self._dropped = 0
        self._dropped_lock = threading.Lock()  # separate from ``_lock``: ``record`` must never wait for the writer
        self._closed = False
        self._close_lock = threading.Lock()
        self._q: queue.Queue[object] = queue.Queue(self.QUEUE_SIZE)
        self._lock = threading.Lock()
        sqlite_schema.ensure_schema(config.sqlite_path)
        # timeout + WAL: several processes (workers) may share one file; per-process stats are not merged in memory,
        # but the SQL upserts are atomic so counts add up across processes.
        self._db = sqlite3.connect(config.sqlite_path, check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SLOW_EXTRA_SCHEMA)
        for ext in self._exts:
            for statement in ext.sqlite_schema():
                self._db.execute(statement)
        self._db.commit()
        self._known = {r[0] for r in self._db.execute("SELECT key FROM routes")}
        self._thread = threading.Thread(target=self._run, name="fastapi-perfstats-writer", daemon=True)
        self._thread.start()

    @property
    def dropped(self) -> int:
        with self._dropped_lock:
            return self._dropped

    def record(self, call: CallRecord) -> None:
        try:
            self._q.put_nowait(call)
        except queue.Full:
            with self._dropped_lock:
                self._dropped += 1

    def flush(self) -> None:
        if not self._thread.is_alive():
            return
        done = threading.Event()
        self._q.put(done)
        done.wait(timeout=10)

    def reset(self) -> None:
        """Delete all statistics. Records enqueued before this call are discarded, later ones are kept."""
        if self._closed:
            raise RuntimeError("cannot reset(): SqliteStore is closed")
        marker = _Reset()
        self._q.put(marker)
        marker.done.wait(timeout=10)
        if marker.error is not None:
            raise marker.error

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        if self._thread.is_alive():
            self._q.put(_STOP)
            self._thread.join(timeout=10)
        with self._lock:
            self._db.close()

    def _run(self) -> None:
        stop = False
        while not stop:
            batch = [self._q.get()]
            try:
                while len(batch) < self.BATCH:
                    batch.append(self._q.get_nowait())
            except queue.Empty:
                pass
            with self._lock:
                pending: list[CallRecord] = []
                for item in batch:
                    if isinstance(item, CallRecord):
                        pending.append(item)
                    elif isinstance(item, _Reset):
                        self._guarded(self._apply, pending)
                        pending = []
                        item.error = self._guarded(self._reset_db)
                self._guarded(self._apply, pending)
            for b in batch:
                if isinstance(b, threading.Event):
                    b.set()
                elif isinstance(b, _Reset):
                    b.done.set()
                elif b is _STOP:
                    stop = True

    def _guarded(self, fn: Callable[..., None], *args: Any) -> Exception | None:
        """Run a unit of writer work; on any failure roll back, restore ``_known``, log and return the error."""
        known = set(self._known)
        try:
            fn(*args)
        except Exception as exc:
            self._db.rollback()
            self._known = known
            log.exception("fastapi-perfstats: store batch rolled back")
            return exc
        return None

    def _reset_db(self) -> None:
        for table in _RESET_TABLES:
            self._db.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
        for ext in self._exts:
            ext.sqlite_reset(self._db)
        self._db.commit()
        self._known.clear()

    def _apply(self, records: list[CallRecord]) -> None:
        if not records:
            return
        touched: set[str] = set()
        for call in records:
            if call.key not in self._known and len(self._known) >= self.cfg.max_routes:
                call = replace(call, key=OTHER_KEY)
            key, ms, status, ts = call.key, call.duration_ms, call.status, call.ts
            self._known.add(key)
            touched.add(key)
            self._db.execute(_UPSERT, (key, 400 <= status < 500, status >= 500, ms, ms, ms, ts, ts))
            self._db.execute("INSERT INTO durations (key, ms) VALUES (?, ?)", (key, ms))
            thr = self.cfg.slow_threshold_for(key)
            if thr is not None and ms >= thr:
                cur = self._db.execute(
                    "INSERT INTO slow_calls (ts, key, ms, status) VALUES (?, ?, ?, ?)", (ts, key, ms, status)
                )
                context = json.dumps(dict(call.context)) if call.context else None
                self._db.execute(
                    "INSERT INTO slow_call_extra (slow_id, query, request_id, context) VALUES (?, ?, ?, ?)",
                    (cur.lastrowid, call.query, call.request_id, context),
                )
            for ext in self._exts:
                ext.sqlite_update(self._db, call)
        for key in touched:
            self._db.execute(_PRUNE, (key, key, self.cfg.window_size))
        self._db.execute(
            "DELETE FROM slow_calls WHERE id <= (SELECT MAX(id) FROM slow_calls) - ?", (self.cfg.max_slow_calls,)
        )
        self._db.execute("DELETE FROM slow_call_extra WHERE slow_id NOT IN (SELECT id FROM slow_calls)")
        now = time.time()
        for ext in self._exts:
            ext.sqlite_prune(self._db, self.cfg, now)
        self._db.commit()

    def routes(self) -> list[RouteStats]:
        self.flush()
        now = time.time()
        with self._lock:
            windows: dict[str, list[float]] = {}
            for key, ms in self._db.execute("SELECT key, ms FROM durations ORDER BY id"):
                windows.setdefault(key, []).append(ms)
            rows = []
            for k, c, e4, e5, tot, lo, hi, first, last in self._db.execute(
                "SELECT key, count, e4, e5, total_ms, min_ms, max_ms, first_ts, last_ts FROM routes"
            ):
                agg = _Agg(deque(windows.get(k, [])), c, e4, e5, tot, lo, hi, first, last)
                rows.append(_make_stats(self.cfg, k, agg))
            finished = _finish(rows, now)
            by_key = {r.key: r for r in finished}
            for ext in self._exts:
                ext.sqlite_finish(by_key, self._db, self.cfg, now)
        return finished

    def slow_calls(self) -> list[SlowCall]:
        self.flush()
        with self._lock:
            cur = self._db.execute(
                "SELECT s.ts, s.key, s.ms, s.status, e.query, e.request_id, e.context FROM slow_calls s "
                "LEFT JOIN slow_call_extra e ON e.slow_id = s.id ORDER BY s.id DESC"
            )
            return [
                SlowCall(ts, key, ms, status, query, rid, json.loads(ctx) if ctx else {})
                for ts, key, ms, status, query, rid, ctx in cur
            ]

    def row_counts(self) -> dict[str, int]:
        """Row counts per table (used to verify the size limits)."""
        self.flush()
        out = {}
        with self._lock:
            for table in ("routes", "durations", "slow_calls", "slow_call_extra"):
                out[table] = self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        return out


def build_store(config: StatsConfig) -> StatsStore:
    if isinstance(config.storage, StatsStore):
        return config.storage
    if config.storage == "memory":
        return MemoryStore(config)
    if config.storage == "sqlite":
        return SqliteStore(config)
    raise ValueError(f"unknown storage {config.storage!r}")
