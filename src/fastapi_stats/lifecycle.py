"""ASGI lifespan handling (owned by #7): close the store on shutdown, show dropped records."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_stats.ext.base import PagePart

if TYPE_CHECKING:
    from fastapi_stats.collector import StatsCollector

_SHUTDOWN_DONE = ("lifespan.shutdown.complete", "lifespan.shutdown.failed")


async def handle_lifespan(collector: StatsCollector, app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> None:
    """Run the lifespan protocol; close the store before the app's shutdown message goes out.

    With ``close_on_shutdown=False`` the app sees the lifespan untouched. ``close()`` may block (the writer thread is
    joined for up to 10 s), so it runs in a worker thread.
    """
    if not collector.config.close_on_shutdown:
        await app(scope, receive, send)
        return

    async def send_wrapper(message: Message) -> None:
        if message["type"] in _SHUTDOWN_DONE:
            await anyio.to_thread.run_sync(collector.close)
        await send(message)

    await app(scope, receive, send_wrapper)


def _html_top(collector: StatsCollector) -> str:
    dropped = collector.store.dropped
    if dropped <= 0:
        return ""
    return f'<p class="bad">{int(dropped)} records dropped (writer queue full)</p>'


PART = PagePart(name="lifecycle", html_top=_html_top)
