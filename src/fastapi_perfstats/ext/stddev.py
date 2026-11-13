"""Error rates (4xx / 5xx share of all calls) and the population standard deviation of durations."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from fastapi_perfstats.config import StatsConfig
from fastapi_perfstats.ext.base import Column, NoopExtension
from fastapi_perfstats.models import CallRecord, RouteStats

_SCHEMA = "CREATE TABLE IF NOT EXISTS ext_stddev (key TEXT PRIMARY KEY, total_sq_ms REAL NOT NULL)"
_UPSERT = (
    "INSERT INTO ext_stddev (key, total_sq_ms) VALUES (?, ?) "
    "ON CONFLICT(key) DO UPDATE SET total_sq_ms = total_sq_ms + excluded.total_sq_ms"
)


def _stddev(total_sq: float, row: RouteStats) -> float:
    """Population stddev ``sqrt(E[x^2] - mean^2)`` from the sum of squares; never negative (rounding)."""
    return math.sqrt(max(total_sq / row.count - row.mean_ms**2, 0.0))


def _apply_rates(row: RouteStats) -> None:
    row.error_rate_4xx = row.errors_4xx / row.count if row.count else 0.0
    row.error_rate_5xx = row.errors_5xx / row.count if row.count else 0.0


def _percent_cell(attr: str) -> Callable[[Any, StatsConfig], tuple[str, float | None]]:
    def cell(row: RouteStats, config: StatsConfig) -> tuple[str, float | None]:
        rate: float = getattr(row, attr)
        return f"{rate * 100:.1f}%", rate

    return cell


def _stddev_cell(row: RouteStats, config: StatsConfig) -> tuple[str, float | None]:
    if row.stddev_ms is None:
        return "n/a", -1.0
    return f"{row.stddev_ms:.2f}", row.stddev_ms


class StddevExtension(NoopExtension):
    def __init__(self) -> None:
        super().__init__("stddev")

    def sqlite_schema(self) -> tuple[str, ...]:
        return (_SCHEMA,)

    def memory_update(self, state: dict[str, Any], call: CallRecord) -> None:
        state["total_sq"] = state.get("total_sq", 0.0) + call.duration_ms**2

    def memory_finish(self, row: RouteStats, state: dict[str, Any], config: StatsConfig, now: float) -> None:
        _apply_rates(row)
        if "total_sq" in state and row.count:
            row.stddev_ms = _stddev(state["total_sq"], row)

    def sqlite_update(self, db: sqlite3.Connection, call: CallRecord) -> None:
        db.execute(_UPSERT, (call.key, call.duration_ms**2))

    def sqlite_finish(
        self, rows: Mapping[str, RouteStats], db: sqlite3.Connection, config: StatsConfig, now: float
    ) -> None:
        for row in rows.values():
            _apply_rates(row)
        for key, total_sq in db.execute("SELECT key, total_sq_ms FROM ext_stddev"):
            found = rows.get(key)
            if found is not None and found.count:
                found.stddev_ms = _stddev(total_sq, found)

    def sqlite_reset(self, db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM ext_stddev")

    def columns(self, config: StatsConfig) -> tuple[Column, ...]:
        return (
            Column("freq", "5xx %", 45, _percent_cell("error_rate_5xx")),
            Column("freq", "4xx %", 65, _percent_cell("error_rate_4xx")),
            Column("rt", "Stddev", 65, _stddev_cell),
        )


EXT = StddevExtension()
