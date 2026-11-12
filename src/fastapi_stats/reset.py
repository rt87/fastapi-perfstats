"""Reset endpoint and page button (owned by #10).

``POST {stats_path}/reset`` clears all statistics. Browsers attach Basic credentials on their own, so a request must
also carry the header ``X-Stats-Reset: 1``: an HTML form cannot set it and a cross-origin ``fetch`` with it needs a
CORS preflight, which this library never allows. Authorization itself is applied by the stats router.

The button posts to ``location.pathname + "/reset"``: the page URL already contains every mount prefix and proxy root
path, so it is right wherever the router is mounted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from fastapi_stats.ext.base import EndpointSpec, PagePart

if TYPE_CHECKING:
    from fastapi_stats.collector import StatsCollector

SUFFIX = "/reset"
HEADER = "x-stats-reset"
BUTTON_ID = "stats-reset"

_JS = (
    "\n(function(){var b=document.getElementById('stats-reset');if(!b){return;}"
    "b.addEventListener('click',function(){"
    "if(!confirm('Reset all statistics? This cannot be undone.')){return;}"
    "fetch(location.pathname.replace(/\\/+$/,'')+'/reset',"
    "{method:'POST',headers:{'X-Stats-Reset':'1'},credentials:'same-origin'})"
    ".then(function(r){if(r.ok){location.reload();}else{alert('Reset failed: HTTP '+r.status);}})"
    ".catch(function(){alert('Reset failed: network error');});});})();\n"
)


def _handler(collector: StatsCollector) -> Callable[[Request], Awaitable[Response]]:
    async def reset(request: Request) -> Response:
        if request.headers.get(HEADER) != "1":
            return PlainTextResponse("Missing header X-Stats-Reset: 1", 400)
        try:
            await run_in_threadpool(collector.reset)
        except NotImplementedError:
            return PlainTextResponse("This store does not support reset", 501)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    return reset


def _html_top(collector: StatsCollector) -> str:
    if not collector.config.allow_reset:
        return ""
    return f'<p><button type="button" id="{BUTTON_ID}">Reset statistics</button></p>'


ENDPOINT = EndpointSpec(suffix=SUFFIX, methods=("POST",), enabled=lambda config: config.allow_reset, handler=_handler)
PART = PagePart(name="reset", html_top=_html_top, js=_JS)
