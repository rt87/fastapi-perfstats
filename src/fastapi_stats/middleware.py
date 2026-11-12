"""Pure ASGI middleware measuring every HTTP request."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_stats import lifecycle

if TYPE_CHECKING:
    from fastapi_stats.collector import StatsCollector


class StatsMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware: no body buffering, no task-per-request overhead)."""

    def __init__(self, app: ASGIApp, collector: StatsCollector) -> None:
        self.app = app
        self.collector = collector

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifecycle.handle_lifespan(self.collector, self.app, scope, receive, send)
            return
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
            self.collector.finish(scope, elapsed, status)  # after the call: scope["route"] is filled in by now
