"""Plain result types and the percentile helper."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

OTHER_KEY = "other"
UNMATCHED_ROUTE = "<unmatched>"
OTHER_METHOD = "OTHER"
KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"})
CLIENT_CLOSED_STATUS = 499


def nearest_rank(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile: the smallest value with at least ``q`` of the data at or below it."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(q * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One finished call, as handed to ``StatsStore.record`` and to extensions."""

    key: str
    duration_ms: float
    status: int
    ts: float  # time.time() when the call finished
    method: str = ""
    path: str = ""  # raw path, never used as a key
    request_bytes: int | None = None
    response_bytes: int | None = None
    query: str | None = None
    request_id: str | None = None
    context: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class SlowExtra:
    """Optional extra detail stored with a slow call."""

    query: str | None = None
    request_id: str | None = None
    context: Mapping[str, str] | None = None


@dataclass
class RecentStats:
    """Aggregates over one recent window (see ``StatsConfig.recent_windows``)."""

    calls: int
    calls_per_min: float
    errors_5xx: int
    error_rate_5xx: float
    mean_ms: float


@dataclass
class RouteStats:
    key: str
    count: int
    errors_4xx: int
    errors_5xx: int
    min_ms: float
    mean_ms: float
    max_ms: float
    quantiles: dict[str, float] = field(default_factory=dict)
    first_ts: float = 0.0
    last_ts: float = 0.0
    share: float = 0.0
    rate_per_min: float = 0.0
    error_rate_4xx: float = 0.0
    error_rate_5xx: float = 0.0
    stddev_ms: float | None = None
    mean_request_bytes: float | None = None
    mean_response_bytes: float | None = None
    max_response_bytes: int | None = None
    recent: dict[str, RecentStats] = field(default_factory=dict)
    series: list[int] = field(default_factory=list)


@dataclass
class SlowCall:
    ts: float
    key: str
    duration_ms: float
    status: int
    query: str | None = None
    request_id: str | None = None
    context: dict[str, str] = field(default_factory=dict)
