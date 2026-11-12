"""fastapi-stats: per-route call statistics, slow-call log and a stats page (prototype)."""

from typing import Any

from fastapi_stats.collector import (
    StatsCollector,
    StatsMiddleware,
    get_default_collector,
    import_route_module,
    set_default_collector,
)
from fastapi_stats.config import DEFAULT_QUANTILES, BasicAuth, StatsConfig
from fastapi_stats.models import OTHER_KEY, RouteStats, SlowCall
from fastapi_stats.page import mount_stats, render_html, render_json, stats_router
from fastapi_stats.stores import MemoryStore, SqliteStore, StatsStore

__all__ = [
    "BasicAuth",
    "DEFAULT_QUANTILES",
    "OTHER_KEY",
    "MemoryStore",
    "RouteStats",
    "SlowCall",
    "SqliteStore",
    "StatsCollector",
    "StatsConfig",
    "StatsMiddleware",
    "StatsStore",
    "StatsRoute",
    "get_default_collector",
    "make_route_class",
    "mount_stats",
    "render_html",
    "render_json",
    "set_default_collector",
    "stats_router",
]


def __getattr__(name: str) -> Any:
    # StatsRoute / make_route_class need fastapi (never a dependency of this package): imported lazily so that
    # `import fastapi_stats` keeps working with starlette only.
    if name in ("StatsRoute", "make_route_class"):
        return getattr(import_route_module(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
