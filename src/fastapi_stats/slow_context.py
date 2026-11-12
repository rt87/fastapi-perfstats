"""Extra detail captured with slow calls (owned by #13)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi_stats.config import StatsConfig
from fastapi_stats.ext.base import NoopExtension
from fastapi_stats.models import SlowExtra

EXT = NoopExtension("slow_context")


def capture(scope: Mapping[str, Any], config: StatsConfig, key: str, duration_ms: float) -> SlowExtra:
    """Query / request id / context for a slow call. Stub: nothing captured."""
    return SlowExtra()
