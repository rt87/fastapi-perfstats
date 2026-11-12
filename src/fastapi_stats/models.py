"""Plain result types and the percentile helper."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

OTHER_KEY = "other"


def nearest_rank(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile: the smallest value with at least ``q`` of the data at or below it."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(q * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


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


@dataclass
class SlowCall:
    ts: float
    key: str
    duration_ms: float
    status: int
