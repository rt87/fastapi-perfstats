"""``StatsRoute``: an ``APIRoute`` that records each call. Requires fastapi."""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.responses import Response

from fastapi_stats.collector import StatsCollector, get_default_collector


class StatsRoute(APIRoute):
    """Use as ``APIRouter(route_class=StatsRoute)``; records into the default collector.

    For your own collector use ``collector.route_class`` (a subclass bound to it).
    """

    collector: StatsCollector | None = None  # None -> process-wide default, resolved per request

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            collector = self.collector or get_default_collector()
            if collector.is_excluded(request.url.path):
                return await original(request)
            start = time.perf_counter()
            status = 500
            try:
                response = await original(request)
                status = response.status_code
                return response
            except Exception as exc:
                status = getattr(exc, "status_code", 500)
                raise
            finally:
                key = collector.key_for(request.scope)
                if key is not None:
                    collector.record(key, (time.perf_counter() - start) * 1000, status)

        return handler


def make_route_class(collector: StatsCollector) -> type[StatsRoute]:
    """A ``StatsRoute`` subclass bound to ``collector``."""
    return type("StatsRoute", (StatsRoute,), {"collector": collector})
