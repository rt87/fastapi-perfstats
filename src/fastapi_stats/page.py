"""HTML + JSON stats page. WARNING: route keys come from request data; every string is escaped."""

from __future__ import annotations

import base64
import binascii
import contextlib
import logging
import secrets
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from html import escape
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route, Router

from fastapi_stats.collector import StatsCollector
from fastapi_stats.config import BasicAuth, quantile_label

log = logging.getLogger("fastapi_stats")

_CSS = """
:root{--bg:#fff;--fg:#1b1f24;--mut:#667;--line:#d0d7de;--head:#f3f5f7;--bad:#b3261e}
@media (prefers-color-scheme:dark){
:root{--bg:#14171a;--fg:#e6e9ec;--mut:#98a2ad;--line:#2f353b;--head:#1d2227;--bad:#ff8a80}}
body{background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif;margin:1rem 2rem}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem}
th,td{border:1px solid var(--line);padding:.3rem .6rem;text-align:right}
th:nth-child(-n+2),td:nth-child(-n+2){text-align:left}
th{background:var(--head);cursor:pointer;user-select:none}
.bad{color:var(--bad)} input{background:var(--bg);color:var(--fg);border:1px solid var(--line);padding:.2rem}
h1,h2{font-weight:600} .mut{color:var(--mut)}
"""

_JS = """
document.querySelectorAll('table.s').forEach(function(t){
  var tb=t.tBodies[0], dir=1;
  t.tHead.querySelectorAll('th').forEach(function(th,i){
    th.addEventListener('click',function(){
      dir=-dir;
      Array.from(tb.rows).sort(function(a,b){
        var x=a.cells[i],y=b.cells[i],p=x.dataset.v,q=y.dataset.v;
        if(p!==undefined&&q!==undefined)return (parseFloat(p)-parseFloat(q))*dir;
        return x.textContent.localeCompare(y.textContent)*dir;
      }).forEach(function(r){tb.appendChild(r);});
    });
  });
});
document.querySelectorAll('input.f').forEach(function(inp){
  inp.addEventListener('input',function(){
    var t=document.getElementById(inp.dataset.t),s=inp.value.toLowerCase();
    Array.from(t.tBodies[0].rows).forEach(function(r){
      r.style.display=r.textContent.toLowerCase().indexOf(s)<0?'none':'';
    });
  });
});
"""


def _check_basic_auth(request: Request, auth: BasicAuth | None) -> Response | None:
    """``None`` if the request may pass, else a 401 response."""
    if auth is None:
        return None
    user, pw = b"", b""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "basic":
        with contextlib.suppress(binascii.Error, ValueError):
            user, _, pw = base64.b64decode(token, validate=True).partition(b":")
    # both compares always run (no short-circuit) so timing does not reveal which part was wrong
    ok_user = secrets.compare_digest(user, auth.username.encode())
    ok_pw = secrets.compare_digest(pw, auth.password.encode())
    if ok_user and ok_pw:
        return None
    return PlainTextResponse("Unauthorized", 401, headers={"WWW-Authenticate": 'Basic realm="stats"'})


def split_key(key: str) -> tuple[str, str]:
    """``"GET /items/{id}"`` -> ``("GET", "/items/{id}")``; keys without a leading method -> ``("", key)``."""
    method, sep, route = key.partition(" ")
    if sep and method.isalpha() and method.isupper():
        return method, route
    return "", key


def _key_cells(key: str) -> str:
    method, route = split_key(key)
    return _td(method) + _td(route)


def _num(v: float, digits: int = 2) -> str:
    return f"{v:.{digits}f}"


def _td(text: object, value: float | None = None, cls: str = "") -> str:
    v = "" if value is None else f' data-v="{value!r}"'
    c = f' class="{cls}"' if cls else ""
    return f"<td{v}{c}>{escape(str(text))}</td>"


def _table(table_id: str, headers: Sequence[str], rows: list[str]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    return (
        f'<input class="f" data-t="{table_id}" placeholder="filter..." aria-label="filter {table_id}">'
        f'<table class="s" id="{table_id}"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )


def _ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def render_html(collector: StatsCollector) -> str:
    cfg = collector.config
    routes = collector.store.routes()
    slow = collector.store.slow_calls()
    labels = [quantile_label(q) for q in cfg.quantiles]

    freq = [
        "<tr>"
        + _key_cells(r.key)
        + _td(r.count, r.count)
        + _td(r.errors_5xx, r.errors_5xx, "bad" if r.errors_5xx else "")
        + _td(r.errors_4xx, r.errors_4xx)
        + _td(f"{r.share * 100:.1f}%", r.share)
        + _td(_num(r.rate_per_min), r.rate_per_min)
        + "</tr>"
        for r in routes
    ]
    by_max = sorted(routes, key=lambda r: r.max_ms, reverse=True)
    runtimes = [
        "<tr>"
        + _key_cells(r.key)
        + _td(_num(r.max_ms), r.max_ms)
        + "".join(_td(_num(r.quantiles[lb]), r.quantiles[lb]) for lb in labels)
        + _td(_num(r.mean_ms), r.mean_ms)
        + _td(_num(r.min_ms), r.min_ms)
        + "</tr>"
        for r in by_max
    ]
    slow_rows = [
        "<tr>"
        + _key_cells(s.key)
        + _td(_ts(s.ts), s.ts)
        + _td(_num(s.duration_ms), s.duration_ms)
        + _td(s.status, s.status, "bad" if s.status >= 500 else "")
        + "</tr>"
        for s in slow
    ]
    thr = "disabled" if cfg.slow_threshold_ms is None else f">= {cfg.slow_threshold_ms:g} ms"
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>API stats</title>'
        f"<style>{_CSS}</style></head><body><h1>API stats</h1>"
        '<p class="mut">Click a header to sort, type to filter. Durations in ms. '
        f"Quantiles over the last {cfg.window_size} calls per route. "
        f'<a href="{escape(cfg.stats_path)}.json">JSON</a></p>'
        "<h2>Call frequency</h2>"
        + _table("freq", ["Method", "Route", "Count", "5xx", "4xx", "Share", "Calls/min"], freq)
        + "<h2>Longest runtimes</h2>"
        + _table("rt", ["Method", "Route", "Max", *[lb for lb in labels], "Mean", "Min"], runtimes)
        + f"<h2>Slow calls ({escape(thr)})</h2>"
        + _table("slow", ["Method", "Route", "Time", "Duration", "Status"], slow_rows)
        + f"<script>{_JS}</script></body></html>"
    )


def _with_parts(d: dict[str, Any]) -> dict[str, Any]:
    d["method"], d["route"] = split_key(d["key"])  # "key" (METHOD + template) stays for compatibility
    return d


def render_json(collector: StatsCollector) -> dict[str, Any]:
    cfg = collector.config
    return {
        "quantiles": [quantile_label(q) for q in cfg.quantiles],
        "slow_threshold_ms": cfg.slow_threshold_ms,
        "routes": [_with_parts(asdict(r)) for r in collector.store.routes()],
        "slow_calls": [_with_parts(asdict(s)) for s in collector.store.slow_calls()],
    }


def stats_router(
    collector: StatsCollector, path: str | None = None, dependencies: Sequence[Any] | None = None
) -> Any:
    """Build a router serving the page at ``path`` and JSON at ``path + ".json"``.

    Without ``dependencies`` this is a plain Starlette ``Router`` (no fastapi import). With FastAPI
    ``dependencies`` (e.g. ``[Depends(require_admin)]``) a FastAPI ``APIRouter`` is built instead (imports fastapi
    only then). Basic auth from the config applies in both cases. The page exposes your route structure and
    timings: do NOT leave it public.
    """
    p = (path or collector.config.stats_path).rstrip("/") or "/"
    auth = collector.config.basic_auth
    if auth is None and not dependencies:
        log.warning("fastapi-stats: stats page at %s is mounted WITHOUT authentication", p)

    async def stats_page(request: Request) -> Response:
        return _check_basic_auth(request, auth) or HTMLResponse(await run_in_threadpool(render_html, collector))

    async def stats_json(request: Request) -> Response:
        return _check_basic_auth(request, auth) or JSONResponse(await run_in_threadpool(render_json, collector))

    if dependencies:
        try:
            from fastapi import APIRouter
        except ImportError as exc:
            raise ImportError("stats_router(dependencies=...) needs FastAPI installed") from exc
        router = APIRouter(dependencies=list(dependencies), include_in_schema=False)
        router.add_api_route(p, stats_page, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route(p + ".json", stats_json, methods=["GET"])
        return router
    return Router(routes=[Route(p, stats_page, methods=["GET"]), Route(p + ".json", stats_json, methods=["GET"])])


def mount_stats(
    app: Any, collector: StatsCollector, path: str | None = None, dependencies: Sequence[Any] | None = None
) -> None:
    """Add the stats routes to a Starlette or FastAPI ``app``."""
    app.router.routes.extend(stats_router(collector, path, dependencies).routes)
