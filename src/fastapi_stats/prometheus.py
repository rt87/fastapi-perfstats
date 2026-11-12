"""Prometheus text export (owned by #17)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from fastapi_stats.ext.base import EndpointSpec

if TYPE_CHECKING:
    from fastapi_stats.collector import StatsCollector


def render_prometheus(collector: StatsCollector) -> str:
    """Prometheus exposition text for the collector's statistics. Stub: empty."""
    return ""


def _handler(collector: StatsCollector) -> Callable[[Request], Awaitable[Response]]:
    async def metrics(request: Request) -> Response:
        return PlainTextResponse(render_prometheus(collector))

    return metrics


ENDPOINT = EndpointSpec(suffix="/metrics", methods=("GET",), enabled=lambda config: config.prometheus, handler=_handler)
