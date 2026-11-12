"""Recent activity: per-minute buckets give 1/5/15-minute rates, error rates and latency (like a load average).

A bucket is one wall-clock minute (``int(ts // 60)``) holding ``count``, ``e5`` (5xx calls) and ``total_ms``.
Memory keeps a bounded deque per route, SQLite the table ``ext_recent`` (summing naturally across worker processes).
Both stores clamp a timestamp that goes backwards (clock change) into the newest bucket of the route, so they agree.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any, NamedTuple

from fastapi_stats.config import StatsConfig
from fastapi_stats.ext.base import Column, NoopExtension
from fastapi_stats.models import CallRecord, RecentStats, RouteStats

MAX_MINUTES = 1440  # upper bound of ``StatsConfig.recent_minutes``; memory buckets never exceed it

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS ext_recent "
    "(key TEXT NOT NULL, minute INTEGER NOT NULL, count INTEGER NOT NULL, e5 INTEGER NOT NULL, "
    "total_ms REAL NOT NULL, PRIMARY KEY (key, minute))"
)
_UPSERT = (
    "INSERT INTO ext_recent (key, minute, count, e5, total_ms) VALUES (?, ?, 1, ?, ?) "
    "ON CONFLICT(key, minute) DO UPDATE SET count = count + 1, e5 = e5 + excluded.e5, "
    "total_ms = total_ms + excluded.total_ms"
)


class Bucket(NamedTuple):
    minute: int
    calls: int
    e5: int
    total_ms: float


def recent_stats(buckets: Iterable[Bucket], now: float, minutes: int, first_ts: float) -> RecentStats:
    """Aggregate the buckets of the minutes ``(now_minute - minutes, now_minute]``.

    ``calls_per_min`` divides by the observed span: the full window, shortened to the time since ``first_ts`` for
    young routes (at least one second, so a burst right after start does not explode).
    """
    now_minute = int(now // 60)
    calls = e5 = 0
    total = 0.0
    for b in buckets:
        if now_minute - minutes < b.minute <= now_minute:
            calls += b.calls
            e5 += b.e5
            total += b.total_ms
    span = max(min((minutes - 1) + (now % 60) / 60, (now - first_ts) / 60), 1 / 60)
    return RecentStats(
        calls=calls,
        calls_per_min=calls / span,
        errors_5xx=e5,
        error_rate_5xx=e5 / calls if calls else 0.0,
        mean_ms=total / calls if calls else 0.0,
    )


def label(minutes: int) -> str:
    return f"{minutes}m"


def _fill(row: RouteStats, buckets: list[Bucket], config: StatsConfig, now: float) -> None:
    if not config.recent_minutes:
        return
    row.recent = {label(w): recent_stats(buckets, now, w, row.first_ts) for w in config.recent_windows}
    now_minute = int(now // 60)
    counts = {b.minute: b.calls for b in buckets}
    row.series = [counts.get(m, 0) for m in range(now_minute - config.recent_minutes + 1, now_minute + 1)]


def _rate_cell(name: str) -> Callable[[Any, StatsConfig], tuple[str, float | None]]:
    def cell(row: RouteStats, config: StatsConfig) -> tuple[str, float | None]:
        stats = row.recent.get(name)
        return ("n/a", -1.0) if stats is None else (f"{stats.calls_per_min:.2f}", stats.calls_per_min)

    return cell


def _error_cell(name: str) -> Callable[[Any, StatsConfig], tuple[str, float | None]]:
    def cell(row: RouteStats, config: StatsConfig) -> tuple[str, float | None]:
        stats = row.recent.get(name)
        return ("n/a", -1.0) if stats is None else (f"{stats.error_rate_5xx * 100:.1f}%", stats.error_rate_5xx)

    return cell


class RecentExtension(NoopExtension):
    def __init__(self) -> None:
        super().__init__("recent")

    def sqlite_schema(self) -> tuple[str, ...]:
        return (_SCHEMA,)

    # -- memory ----------------------------------------------------------------------------------------------
    def memory_update(self, state: dict[str, Any], call: CallRecord) -> None:
        # The hook has no config: keep at most MAX_MINUTES buckets; ``memory_finish`` trims to ``recent_minutes``.
        buckets: deque[list[float]] = state.setdefault("buckets", deque(maxlen=MAX_MINUTES))
        minute = int(call.ts // 60)
        e5 = call.status >= 500
        if buckets and minute <= buckets[-1][0]:  # same minute, or the clock went backwards
            last = buckets[-1]
            last[1] += 1
            last[2] += e5
            last[3] += call.duration_ms
        else:
            buckets.append([minute, 1, e5, call.duration_ms])

    def memory_finish(self, row: RouteStats, state: dict[str, Any], config: StatsConfig, now: float) -> None:
        buckets = state.get("buckets")
        if not config.recent_minutes:
            if buckets is not None:
                buckets.clear()  # nothing is kept when the feature is off
            return
        if buckets is None:
            return
        while len(buckets) > config.recent_minutes:
            buckets.popleft()
        _fill(row, [Bucket(int(m), int(c), int(e), t) for m, c, e, t in buckets], config, now)

    # -- sqlite ----------------------------------------------------------------------------------------------
    def sqlite_update(self, db: sqlite3.Connection, call: CallRecord) -> None:
        minute = int(call.ts // 60)
        newest = db.execute("SELECT MAX(minute) FROM ext_recent WHERE key = ?", (call.key,)).fetchone()[0]
        if newest is not None and minute < newest:  # clock went backwards: count into the newest bucket
            minute = newest
        db.execute(_UPSERT, (call.key, minute, call.status >= 500, call.duration_ms))

    def sqlite_prune(self, db: sqlite3.Connection, config: StatsConfig, now: float) -> None:
        if not config.recent_minutes:
            db.execute("DELETE FROM ext_recent")
            return
        db.execute("DELETE FROM ext_recent WHERE minute <= ?", (int(now // 60) - config.recent_minutes,))

    def sqlite_finish(
        self, rows: Mapping[str, RouteStats], db: sqlite3.Connection, config: StatsConfig, now: float
    ) -> None:
        if not config.recent_minutes:
            return
        by_key: dict[str, list[Bucket]] = {}
        for key, minute, count, e5, total in db.execute("SELECT key, minute, count, e5, total_ms FROM ext_recent"):
            by_key.setdefault(key, []).append(Bucket(minute, count, e5, total))
        for key, row in rows.items():
            _fill(row, by_key.get(key, []), config, now)

    def sqlite_reset(self, db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM ext_recent")

    # -- page ------------------------------------------------------------------------------------------------
    def columns(self, config: StatsConfig) -> tuple[Column, ...]:
        if not config.recent_minutes or not config.recent_windows:
            return ()
        cols = [
            Column("freq", f"Calls/min ({label(w)})", min(91 + i, 98), _rate_cell(label(w)))
            for i, w in enumerate(config.recent_windows)
        ]
        biggest = label(config.recent_windows[-1])
        cols.append(Column("freq", f"5xx % ({biggest})", 99, _error_cell(biggest)))
        return tuple(cols)


EXT = RecentExtension()
