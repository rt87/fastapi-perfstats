"""fastapi-stats: per-route call statistics, slow-call log and a stats page (prototype)."""

from fastapi_stats.collector import StatsCollector, StatsMiddleware, make_route_class
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
    "make_route_class",
    "mount_stats",
    "render_html",
    "render_json",
    "stats_router",
]
