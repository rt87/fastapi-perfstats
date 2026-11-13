"""Shared test helpers: build domain objects with sensible defaults and record calls without a real store."""

from __future__ import annotations

from typing import Any

from fastapi_perfstats.models import CallRecord, RouteStats, SlowCall
from fastapi_perfstats.stores import StatsStore

FIXED_TS = 1_700_000_000.0


def make_call(
    key: str = "GET /items/{item_id}",
    duration_ms: float = 10.0,
    status: int = 200,
    ts: float = FIXED_TS,
    **fields: Any,
) -> CallRecord:
    return CallRecord(key=key, duration_ms=duration_ms, status=status, ts=ts, **fields)


def make_route_stats(key: str = "GET /items/{item_id}", **fields: Any) -> RouteStats:
    values: dict[str, Any] = {
        "count": 1,
        "errors_4xx": 0,
        "errors_5xx": 0,
        "min_ms": 10.0,
        "mean_ms": 10.0,
        "max_ms": 10.0,
        "first_ts": FIXED_TS,
        "last_ts": FIXED_TS,
    }
    values.update(fields)
    return RouteStats(key=key, **values)


def make_slow_call(key: str = "GET /items/{item_id}", **fields: Any) -> SlowCall:
    values: dict[str, Any] = {"ts": FIXED_TS, "duration_ms": 600.0, "status": 200}
    values.update(fields)
    return SlowCall(key=key, **values)


class RecordingStore(StatsStore):
    """Keeps every recorded ``CallRecord`` in ``records``; reads return nothing."""

    def __init__(self) -> None:
        self.records: list[CallRecord] = []

    def record(self, call: CallRecord) -> None:
        self.records.append(call)

    def routes(self) -> list[RouteStats]:
        return []

    def slow_calls(self) -> list[SlowCall]:
        return []
