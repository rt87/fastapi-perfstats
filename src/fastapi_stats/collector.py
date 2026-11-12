"""Collector: ties config + store together and offers the three ways to measure (middleware, route class, decorator)."""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_stats.config import StatsConfig
from fastapi_stats.stores import StatsStore, build_store


def _is_excluded(path: str, excluded: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in excluded if p)


class StatsCollector:
    """Owns the store. Create one per app and hand it to the middleware / route class / stats router."""

    def __init__(self, config: StatsConfig | None = None) -> None:
        self.config = config or StatsConfig()
        self.store: StatsStore = build_store(self.config)

    # -- recording -------------------------------------------------------------------------------------------
    def is_excluded(self, path: str) -> bool:
        return _is_excluded(path, self.config.excluded)

    def key_for(self, scope: Mapping[str, Any]) -> str | None:
        """Key = METHOD + route template. Raw path only when no route matched (bounded by max_routes/"other")."""
        if self.config.key_func is not None:
            key = self.config.key_func(scope)
        else:
            route = scope.get("route")  # set by routing, i.e. only valid *after* the app has run
            path = getattr(route, "path", None) or scope.get("path", "")
            key = f"{scope.get('method', 'GET')} {path}"
        return None if key is None else key[: self.config.max_key_length]

    def record(self, key: str, duration_ms: float, status: int) -> None:
        self.store.record(key, duration_ms, status)

    # -- app/router level ------------------------------------------------------------------------------------
    @property
    def middleware(self) -> type:
        """``app.add_middleware(collector.middleware)``-friendly class bound to this collector."""
        collector = self

        class _Bound(StatsMiddleware):
            def __init__(self, app: ASGIApp) -> None:
                super().__init__(app, collector)

        return _Bound

    # -- single routes ---------------------------------------------------------------------------------------
    @property
    def route_class(self) -> type:
        """``APIRouter(route_class=collector.route_class)`` or ``@router.get(..., route_class_override=...)``."""
        from fastapi_stats.route import make_route_class  # lazy: needs fastapi

        return make_route_class(self)

    def track(
        self, name: str | None = None
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        """Decorator for one async endpoint: ``@app.get("/x")`` on top, ``@collector.track()`` below it."""

        def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            label = name or fn.__name__

            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                status = 200
                try:
                    result = await fn(*args, **kwargs)
                    status = getattr(result, "status_code", 200)
                    return result
                except Exception as exc:
                    status = getattr(exc, "status_code", 500)
                    raise
                finally:
                    self.record(label, (time.perf_counter() - start) * 1000, status)

            return wrapper

        return deco


class StatsMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware: no body buffering, no task-per-request overhead)."""

    def __init__(self, app: ASGIApp, collector: StatsCollector) -> None:
        self.app = app
        self.collector = collector

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.collector.is_excluded(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        status = 500  # stays 500 if the app raises before sending a response
        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                status = message["status"]
                started = True
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            if not started:
                status = 500
            key = self.collector.key_for(scope)  # after the call: scope["route"] is filled in by now
            if key is not None:
                self.collector.record(key, elapsed, status)


_default: StatsCollector | None = None


def set_default_collector(collector: StatsCollector | None) -> None:
    """Set (or with ``None`` reset) the process-wide collector used by the plain ``StatsRoute`` class."""
    global _default
    _default = collector


def get_default_collector() -> StatsCollector:
    """The process-wide collector; created with a default ``StatsConfig`` on first use."""
    global _default
    if _default is None:
        _default = StatsCollector()
    return _default
