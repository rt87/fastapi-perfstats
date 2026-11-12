"""Pure ASGI middleware measuring every HTTP request."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_stats import lifecycle
from fastapi_stats.models import CLIENT_CLOSED_STATUS

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
        completed = False  # the final body chunk was handed to the server
        disconnected = False
        end = 0.0
        track_sizes = self.collector.config.track_sizes
        count_body = scope.get("method") != "HEAD"  # servers drop the body of a HEAD response: it is not sent
        request_bytes = 0
        response_bytes = 0

        async def receive_wrapper() -> Message:
            nonlocal disconnected, request_bytes
            message = await receive()
            if message["type"] == "http.disconnect":
                if not completed:
                    disconnected = True
            elif track_sizes and message["type"] == "http.request":
                request_bytes += len(message.get("body", b""))
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal status, started, completed, end, response_bytes
            if message["type"] == "http.response.start":
                status = message["status"]
                started = True
            elif message["type"] == "http.response.body":
                if track_sizes and count_body:
                    response_bytes += len(message.get("body", b""))
                if not completed and not message.get("more_body", False):
                    end = time.perf_counter()
                    completed = True
            await send(message)

        start = time.perf_counter()
        failed = True
        try:
            await self.app(scope, receive_wrapper, send_wrapper)
            failed = False
        finally:
            elapsed = ((end if completed else time.perf_counter()) - start) * 1000
            if disconnected and not completed:
                status = CLIENT_CLOSED_STATUS
            elif failed and not completed:
                status = 500  # never started, or the stream broke after the status line went out
            self.collector.finish(
                scope,
                elapsed,
                status,
                request_bytes=request_bytes if track_sizes else None,
                response_bytes=response_bytes if track_sizes else None,
            )  # after the call: scope["route"] is filled in by now
