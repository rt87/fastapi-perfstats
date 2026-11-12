"""Collector: ties config + store together and offers the three ways to measure (middleware, route class, decorator)."""

from __future__ import annotations

import functools
import importlib
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from starlette.types import ASGIApp

# `ext` must load before `slow_context` (the extension registry imports slow_context back); names load in this order
from fastapi_stats import ext, keys, mounts, slow_context  # noqa: F401
from fastapi_stats.config import StatsConfig
from fastapi_stats.middleware import StatsMiddleware
from fastapi_stats.models import CallRecord, SlowExtra
from fastapi_stats.stores import StatsStore, build_store

if TYPE_CHECKING:  # fastapi is only needed for typing here; the runtime import stays lazy
    from types import ModuleType

    from fastapi_stats.route import StatsRoute


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

    def finish(
        self,
        scope: Mapping[str, Any],
        duration_ms: float,
        status: int,
        *,
        request_bytes: int | None = None,
        response_bytes: int | None = None,
    ) -> None:
        """Record one finished request. Call after the app ran: ``scope["route"]`` is filled in by routing."""
        key = keys.build_key(scope, self.config, mounts.mount_prefix(scope))  # key_func, if set, decides there
        if key is None:
            return
        key = key[: self.config.max_key_length]
        threshold = self.config.slow_threshold_for(key)
        slow = threshold is not None and duration_ms >= threshold
        extra = slow_context.capture(scope, self.config, key, duration_ms) if slow else SlowExtra()
        self.record_call(
            CallRecord(
                key=key,
                duration_ms=duration_ms,
                status=status,
                ts=time.time(),
                method=scope.get("method", ""),
                path=scope.get("path", ""),
                request_bytes=request_bytes,
                response_bytes=response_bytes,
                query=extra.query,
                request_id=extra.request_id,
                context=extra.context,
            )
        )

    def record_call(self, call: CallRecord) -> None:
        self.store.record(call)

    def record(self, key: str, duration_ms: float, status: int) -> None:
        self.record_call(CallRecord(key=key, duration_ms=duration_ms, status=status, ts=time.time()))

    def reset(self) -> None:
        """Forget all statistics (stores that do not support it raise ``NotImplementedError``)."""
        self.store.reset()

    def close(self) -> None:
        self.store.close()

    # -- app/router level ------------------------------------------------------------------------------------
    @property
    def middleware(self) -> Callable[[ASGIApp], ASGIApp]:
        """``app.add_middleware(collector.middleware)``-friendly class bound to this collector."""
        collector = self

        class _Bound(StatsMiddleware):
            def __init__(self, app: ASGIApp) -> None:
                super().__init__(app, collector)

        return _Bound

    # -- single routes ---------------------------------------------------------------------------------------
    @property
    def route_class(self) -> type[StatsRoute]:
        """``APIRouter(route_class=collector.route_class)`` or ``@router.get(..., route_class_override=...)``."""
        route_cls: type[StatsRoute] = import_route_module().make_route_class(self)
        return route_cls

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
                    self.record_call(
                        CallRecord(
                            key=label, duration_ms=(time.perf_counter() - start) * 1000, status=status, ts=time.time()
                        )
                    )

            return wrapper

        return deco


def import_route_module() -> ModuleType:
    """Import ``fastapi_stats.route`` (needs FastAPI) with a clear error if FastAPI is missing."""
    try:
        return importlib.import_module("fastapi_stats.route")
    except ImportError as exc:
        raise ImportError("StatsRoute needs FastAPI installed (pip install fastapi)") from exc


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
