"""ASGI lifespan handling (owned by #7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.types import ASGIApp, Receive, Scope, Send

from fastapi_stats.ext.base import PagePart

if TYPE_CHECKING:
    from fastapi_stats.collector import StatsCollector


async def handle_lifespan(collector: StatsCollector, app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> None:
    """Run the lifespan protocol. Stub: pass-through."""
    await app(scope, receive, send)


PART = PagePart(name="lifecycle", html_top=lambda collector: "")
