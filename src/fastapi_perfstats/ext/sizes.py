"""Request and response payload sizes per route (mean request, mean response, max response)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from fastapi_perfstats.config import StatsConfig
from fastapi_perfstats.ext.base import Column, NoopExtension
from fastapi_perfstats.models import CallRecord, RouteStats

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS ext_sizes (key TEXT PRIMARY KEY, sized_count INTEGER NOT NULL, "
    "req_total INTEGER NOT NULL, resp_total INTEGER NOT NULL, resp_max INTEGER NOT NULL)"
)
_UPSERT = (
    "INSERT INTO ext_sizes (key, sized_count, req_total, resp_total, resp_max) VALUES (?, 1, ?, ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET sized_count = sized_count + 1, req_total = req_total + excluded.req_total, "
    "resp_total = resp_total + excluded.resp_total, resp_max = MAX(resp_max, excluded.resp_max)"
)

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def _bytes(n: float) -> str:
    """Human readable size: ``512 B``, ``1.5 KiB``, ``2.0 MiB`` (binary units)."""
    value = float(n)
    unit = 0
    while value >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    return f"{value:.0f} B" if unit == 0 else f"{value:.1f} {_UNITS[unit]}"


def _size_cell(value: float | None) -> tuple[str, float | None]:
    if value is None:
        return "n/a", -1.0
    return _bytes(value), value


def _mean_request(row: RouteStats, config: StatsConfig) -> tuple[str, float | None]:
    return _size_cell(row.mean_request_bytes)


def _mean_response(row: RouteStats, config: StatsConfig) -> tuple[str, float | None]:
    return _size_cell(row.mean_response_bytes)


def _max_response(row: RouteStats, config: StatsConfig) -> tuple[str, float | None]:
    return _size_cell(row.max_response_bytes)


def _apply(row: RouteStats, count: int, req_total: int, resp_total: int, resp_max: int) -> None:
    if count <= 0:
        return
    row.mean_request_bytes = req_total / count
    row.mean_response_bytes = resp_total / count
    row.max_response_bytes = resp_max


class SizesExtension(NoopExtension):
    """Only calls with both sizes known contribute; routes without any sized call keep ``None``."""

    def __init__(self) -> None:
        super().__init__("sizes")

    def sqlite_schema(self) -> tuple[str, ...]:
        return (_SCHEMA,)

    def memory_update(self, state: dict[str, Any], call: CallRecord) -> None:
        if call.request_bytes is None or call.response_bytes is None:
            return
        state["sized_count"] = state.get("sized_count", 0) + 1
        state["req_total"] = state.get("req_total", 0) + call.request_bytes
        state["resp_total"] = state.get("resp_total", 0) + call.response_bytes
        state["resp_max"] = max(state.get("resp_max", 0), call.response_bytes)

    def memory_finish(self, row: RouteStats, state: dict[str, Any], config: StatsConfig, now: float) -> None:
        count = state.get("sized_count", 0)
        _apply(row, count, state.get("req_total", 0), state.get("resp_total", 0), state.get("resp_max", 0))

    def sqlite_update(self, db: sqlite3.Connection, call: CallRecord) -> None:
        if call.request_bytes is None or call.response_bytes is None:
            return
        db.execute(_UPSERT, (call.key, call.request_bytes, call.response_bytes, call.response_bytes))

    def sqlite_finish(
        self, rows: Mapping[str, RouteStats], db: sqlite3.Connection, config: StatsConfig, now: float
    ) -> None:
        for key, count, req_total, resp_total, resp_max in db.execute(
            "SELECT key, sized_count, req_total, resp_total, resp_max FROM ext_sizes"
        ):
            found = rows.get(key)
            if found is not None:
                _apply(found, count, req_total, resp_total, resp_max)

    def sqlite_reset(self, db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM ext_sizes")

    def columns(self, config: StatsConfig) -> tuple[Column, ...]:
        return (
            Column("sizes", "Mean request", 30, _mean_request),
            Column("sizes", "Mean response", 40, _mean_response),
            Column("sizes", "Max response", 50, _max_response),
        )


EXT = SizesExtension()
