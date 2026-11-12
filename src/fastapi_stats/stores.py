"""Storage backends: in-memory (default) and SQLite (stdlib), behind one ABC."""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass

from fastapi_stats.config import StatsConfig, quantile_label
from fastapi_stats.models import OTHER_KEY, CallRecord, RouteStats, SlowCall, nearest_rank
from fastapi_stats.sqlite_schema import SCHEMA


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


def _finish(rows: list[RouteStats]) -> list[RouteStats]:
    total = sum(r.count for r in rows) or 1
    now = time.time()
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

    def __init__(self, config: StatsConfig) -> None:
        self.cfg = config
        self._lock = threading.Lock()
        self._aggs: dict[str, _Agg] = {}
        self._slow: deque[SlowCall] = deque(maxlen=config.max_slow_calls)

    def record(self, call: CallRecord) -> None:
        key, duration_ms, status, ts = call.key, call.duration_ms, call.status, call.ts
        with self._lock:
            if key not in self._aggs and len(self._aggs) >= self.cfg.max_routes and key != OTHER_KEY:
                key = OTHER_KEY
            agg = self._aggs.get(key)
            if agg is None:
                agg = self._aggs[key] = _Agg(window=deque(maxlen=self.cfg.window_size), first=ts)
            agg.count += 1
            agg.e5 += status >= 500
            agg.e4 += 400 <= status < 500
            agg.total += duration_ms
            agg.lo = min(agg.lo, duration_ms)
            agg.hi = max(agg.hi, duration_ms)
            agg.last = ts
            agg.window.append(duration_ms)
            thr = self.cfg.slow_threshold_ms
            if thr is not None and duration_ms >= thr:
                self._slow.append(SlowCall(ts, key, duration_ms, status))

    def routes(self) -> list[RouteStats]:
        with self._lock:
            rows = [_make_stats(self.cfg, k, a) for k, a in self._aggs.items()]
        return _finish(rows)

    def slow_calls(self) -> list[SlowCall]:
        with self._lock:
            return list(reversed(self._slow))  # newest first


_STOP = object()
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


class SqliteStore(StatsStore):
    """Persistent store. ``record`` only enqueues; one writer thread applies batches in one transaction.

    The queue is bounded (``QUEUE_SIZE``); if the writer cannot keep up, new records are dropped
    (counted in ``dropped``) instead of blocking the event loop or growing memory without limit.
    Size limits: ``window_size`` durations per route, ``max_routes`` routes (rest -> "other"),
    ``max_slow_calls`` slow-call rows. Reads call ``flush()`` first, so they see all prior records.
    """

    QUEUE_SIZE = 10_000
    BATCH = 500

    def __init__(self, config: StatsConfig) -> None:
        self.cfg = config
        self._dropped = 0
        self._q: queue.Queue[object] = queue.Queue(self.QUEUE_SIZE)
        self._lock = threading.Lock()
        # timeout + WAL: several processes (workers) may share one file; per-process stats are not merged in memory,
        # but the SQL upserts are atomic so counts add up across processes.
        self._db = sqlite3.connect(config.sqlite_path, check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._known = {r[0] for r in self._db.execute("SELECT key FROM routes")}
        self._thread = threading.Thread(target=self._run, name="fastapi-stats-writer", daemon=True)
        self._thread.start()

    @property
    def dropped(self) -> int:
        return self._dropped

    def record(self, call: CallRecord) -> None:
        item = (call.key, call.duration_ms, call.status, call.ts)
        try:
            self._q.put_nowait(item)
        except queue.Full:
            self._dropped += 1

    def flush(self) -> None:
        if not self._thread.is_alive():
            return
        done = threading.Event()
        self._q.put(done)
        done.wait(timeout=10)

    def close(self) -> None:
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
            records = [b for b in batch if isinstance(b, tuple)]
            with self._lock:
                try:
                    self._apply(records)
                except sqlite3.Error:
                    self._db.rollback()
            for b in batch:
                if isinstance(b, threading.Event):
                    b.set()
                elif b is _STOP:
                    stop = True

    def _apply(self, records: list[tuple[str, float, int, float]]) -> None:
        if not records:
            return
        touched: set[str] = set()
        thr = self.cfg.slow_threshold_ms
        for key, ms, status, ts in records:
            if key not in self._known and len(self._known) >= self.cfg.max_routes:
                key = OTHER_KEY
            self._known.add(key)
            touched.add(key)
            self._db.execute(_UPSERT, (key, 400 <= status < 500, status >= 500, ms, ms, ms, ts, ts))
            self._db.execute("INSERT INTO durations (key, ms) VALUES (?, ?)", (key, ms))
            if thr is not None and ms >= thr:
                self._db.execute(
                    "INSERT INTO slow_calls (ts, key, ms, status) VALUES (?, ?, ?, ?)", (ts, key, ms, status)
                )
        for key in touched:
            self._db.execute(_PRUNE, (key, key, self.cfg.window_size))
        self._db.execute(
            "DELETE FROM slow_calls WHERE id <= (SELECT MAX(id) FROM slow_calls) - ?", (self.cfg.max_slow_calls,)
        )
        self._db.commit()

    def routes(self) -> list[RouteStats]:
        self.flush()
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
        return _finish(rows)

    def slow_calls(self) -> list[SlowCall]:
        self.flush()
        with self._lock:
            cur = self._db.execute("SELECT ts, key, ms, status FROM slow_calls ORDER BY id DESC")
            return [SlowCall(*r) for r in cur]

    def row_counts(self) -> dict[str, int]:
        """Row counts per table (used to verify the size limits)."""
        self.flush()
        out = {}
        with self._lock:
            for table in ("routes", "durations", "slow_calls"):
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
