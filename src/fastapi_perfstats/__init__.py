"""fastapi-perfstats: per-route call statistics, slow-call log and a stats page (prototype)."""

import importlib.metadata
from typing import Any

from fastapi_perfstats.auth import localhost_only
from fastapi_perfstats.collector import (
    StatsCollector,
    get_default_collector,
    import_route_module,
    set_default_collector,
)
from fastapi_perfstats.config import DEFAULT_QUANTILES, AccessCheck, BasicAuth, StatsConfig
from fastapi_perfstats.middleware import StatsMiddleware
from fastapi_perfstats.models import (
    CLIENT_CLOSED_STATUS,
    OTHER_KEY,
    OTHER_METHOD,
    UNMATCHED_ROUTE,
    CallRecord,
    RecentStats,
    RouteStats,
    SlowCall,
    SlowExtra,
)
from fastapi_perfstats.page import mount_stats, render_html, render_json, stats_router
from fastapi_perfstats.prometheus import render_prometheus
from fastapi_perfstats.stores import MemoryStore, SqliteStore, StatsStore

try:
    __version__ = importlib.metadata.version("fastapi-perfstats")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover - only when run from an uninstalled checkout
    __version__ = "0+unknown"

__all__ = [
    "CLIENT_CLOSED_STATUS",
    "DEFAULT_QUANTILES",
    "OTHER_KEY",
    "OTHER_METHOD",
    "UNMATCHED_ROUTE",
    "AccessCheck",
    "BasicAuth",
    "CallRecord",
    "MemoryStore",
    "RecentStats",
    "RouteStats",
    "SlowCall",
    "SlowExtra",
    "SqliteStore",
    "StatsCollector",
    "StatsConfig",
    "StatsMiddleware",
    "StatsStore",
    "StatsRoute",
    "__version__",
    "get_default_collector",
    "localhost_only",
    "make_route_class",
    "mount_stats",
    "render_html",
    "render_json",
    "render_prometheus",
    "set_default_collector",
    "stats_router",
]


def __getattr__(name: str) -> Any:
    # StatsRoute / make_route_class need fastapi (never a dependency of this package): imported lazily so that
    # `import fastapi_perfstats` keeps working with starlette only.
    if name in ("StatsRoute", "make_route_class"):
        return getattr(import_route_module(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
