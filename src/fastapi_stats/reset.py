"""Reset endpoint (owned by #10)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from fastapi_stats.ext.base import EndpointSpec, PagePart

if TYPE_CHECKING:
    from fastapi_stats.collector import StatsCollector


def _handler(collector: StatsCollector) -> Callable[[Request], Awaitable[Response]]:
    async def reset(request: Request) -> Response:
        return PlainTextResponse("Not implemented", 501)

    return reset


ENDPOINT = EndpointSpec(suffix="/reset", methods=("POST",), enabled=lambda config: config.allow_reset, handler=_handler)
PART = PagePart(name="reset", html_top=lambda collector: "")
