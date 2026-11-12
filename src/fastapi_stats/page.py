"""HTML + JSON stats page. WARNING: route keys come from request data; every string is escaped."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html import escape
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, Router

from fastapi_stats import auth, ext, headers, parts
from fastapi_stats.collector import StatsCollector
from fastapi_stats.config import StatsConfig, quantile_label
from fastapi_stats.ext.base import Column, EndpointSpec
from fastapi_stats.models import RouteStats, SlowCall

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


def split_key(key: str) -> tuple[str, str]:
    """``"GET /items/{id}"`` -> ``("GET", "/items/{id}")``; keys without a leading method -> ``("", key)``."""
    method, sep, route = key.partition(" ")
    if sep and method.isalpha() and method.isupper():
        return method, route
    return "", key


def _num(v: float, digits: int = 2) -> str:
    return f"{v:.{digits}f}"


def _td(text: object, value: float | None = None, cls: str = "", raw: bool = False) -> str:
    v = "" if value is None else f' data-v="{value!r}"'
    c = f' class="{cls}"' if cls else ""
    return f"<td{v}{c}>{text if raw else escape(str(text))}</td>"


@dataclass(frozen=True)
class _CoreColumn(Column):
    """A core column; ``cls`` yields the CSS class of its cell (extension columns have none)."""

    cls: Callable[[Any], str] = lambda item: ""


def _method(item: RouteStats | SlowCall, config: StatsConfig) -> tuple[str, float | None]:
    return split_key(item.key)[0], None


def _route(item: RouteStats | SlowCall, config: StatsConfig) -> tuple[str, float | None]:
    return split_key(item.key)[1], None


def _key_columns(table: Any) -> list[Column]:
    return [Column(table, "Method", 10, _method), Column(table, "Route", 20, _route)]


def _quantile_cell(label: str) -> Callable[[Any, StatsConfig], tuple[str, float | None]]:
    return lambda r, config: (_num(r.quantiles[label]), r.quantiles[label])


def core_columns(config: StatsConfig) -> list[Column]:
    """The built-in columns of every table (extensions add theirs through ``Extension.columns``)."""
    rate_header = "Calls/min (all)" if config.recent_minutes > 0 else "Calls/min"
    cols: list[Column] = [
        *_key_columns("freq"),
        Column("freq", "Count", 30, lambda r, c: (str(r.count), r.count)),
        _CoreColumn(
            "freq",
            "5xx",
            40,
            lambda r, c: (str(r.errors_5xx), r.errors_5xx),
            cls=lambda r: "bad" if r.errors_5xx else "",
        ),
        Column("freq", "4xx", 60, lambda r, c: (str(r.errors_4xx), r.errors_4xx)),
        Column("freq", "Share", 80, lambda r, c: (f"{r.share * 100:.1f}%", r.share)),
        Column("freq", rate_header, 90, lambda r, c: (_num(r.rate_per_min), r.rate_per_min)),
        *_key_columns("rt"),
        Column("rt", "Max", 30, lambda r, c: (_num(r.max_ms), r.max_ms)),
        Column("rt", "Mean", 60, lambda r, c: (_num(r.mean_ms), r.mean_ms)),
        Column("rt", "Min", 70, lambda r, c: (_num(r.min_ms), r.min_ms)),
        *_key_columns("slow"),
        Column("slow", "Time", 30, lambda s, c: (_ts(s.ts), s.ts)),
        Column("slow", "Duration", 40, lambda s, c: (_num(s.duration_ms), s.duration_ms)),
        _CoreColumn(
            "slow", "Status", 50, lambda s, c: (str(s.status), s.status), cls=lambda s: "bad" if s.status >= 500 else ""
        ),
        *_key_columns("sizes"),
    ]
    cols += [
        Column("rt", quantile_label(q), 40 + i, _quantile_cell(quantile_label(q)))
        for i, q in enumerate(config.quantiles)
    ]
    return cols


def _columns_for(table: str, config: StatsConfig) -> list[Column]:
    cols = [c for c in core_columns(config) if c.table == table]
    for extension in ext.EXTENSIONS:
        cols += [c for c in extension.columns(config) if c.table == table]
    return sorted(cols, key=lambda c: c.order)


def _table(table_id: str, columns: Sequence[Column], items: Sequence[Any], config: StatsConfig) -> str:
    head = "".join(f"<th>{escape(c.header)}</th>" for c in columns)
    rows = []
    for item in items:
        cells = []
        for c in columns:
            text, value = c.cell(item, config)
            cls = c.cls(item) if isinstance(c, _CoreColumn) else ""
            cells.append(_td(text, value, cls, c.raw_html))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        f'<input class="f" data-t="{table_id}" placeholder="filter..." aria-label="filter {table_id}">'
        f'<table class="s" id="{table_id}"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )


def _ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def style_text() -> str:
    """The inline stylesheet of the page."""
    return _CSS


def script_text(collector: StatsCollector) -> str:
    """The inline script of the page: the core script followed by the script of every page part, in order."""
    return _JS + "".join(part.js for part in parts.PAGE_PARTS)


def _body_attrs(config: StatsConfig) -> str:
    merged: dict[str, str] = {}
    for part in parts.PAGE_PARTS:
        merged.update(part.body_attrs(config))
    return "".join(f' {escape(k)}="{escape(v)}"' for k, v in merged.items())


def render_html(collector: StatsCollector) -> str:
    cfg = collector.config
    routes = collector.store.routes()
    slow = collector.store.slow_calls()
    by_max = sorted(routes, key=lambda r: r.max_ms, reverse=True)
    sizes_columns = _columns_for("sizes", cfg)
    sizes_table = ""
    if len(sizes_columns) > 2:  # more than Method + Route: some extension contributes columns
        sizes_table = "<h2>Payload sizes</h2>" + _table("sizes", sizes_columns, routes, cfg)
    tops = "".join(part.html_top(collector) for part in parts.PAGE_PARTS)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>API stats</title>'
        f"<style>{style_text()}</style></head><body{_body_attrs(cfg)}><h1>API stats</h1>"
        '<p class="mut">Click a header to sort, type to filter. Durations in ms. '
        f"Quantiles over the last {cfg.window_size} calls per route. "
        f'<a href="{escape(cfg.stats_path)}.json">JSON</a></p>'
        + tops
        + "<h2>Call frequency</h2>"
        + _table("freq", _columns_for("freq", cfg), routes, cfg)
        + "<h2>Longest runtimes</h2>"
        + _table("rt", _columns_for("rt", cfg), by_max, cfg)
        + f"<h2>Slow calls ({escape(cfg.slow_threshold_summary())})</h2>"
        + _table("slow", _columns_for("slow", cfg), slow, cfg)
        + sizes_table
        + f"<script>{script_text(collector)}</script></body></html>"
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
        "dropped": collector.store.dropped,
        "slow_thresholds": dict(cfg.slow_thresholds),
    }


def stats_router(collector: StatsCollector, path: str | None = None, dependencies: Sequence[Any] | None = None) -> Any:
    """Build a router serving the page at ``path`` and JSON at ``path + ".json"``.

    Without ``dependencies`` this is a plain Starlette ``Router`` (no fastapi import). With FastAPI
    ``dependencies`` (e.g. ``[Depends(require_admin)]``) a FastAPI ``APIRouter`` is built instead (imports fastapi
    only then). Basic auth from the config applies in both cases. The page exposes your route structure and
    timings: do NOT leave it public.
    """
    config = collector.config
    p = (path or config.stats_path).rstrip("/") or "/"
    if config.credentials is None and config.access is None and not dependencies:
        log.warning("fastapi-stats: stats page at %s is mounted WITHOUT authentication", p)

    async def denied(request: Request) -> Response | None:
        response = await auth.authorize(request, config)
        if response is not None:
            response.headers.update(headers.data_headers())
        return response

    async def stats_page(request: Request) -> Response:
        if (response := await denied(request)) is not None:
            return response
        html = await run_in_threadpool(render_html, collector)
        return HTMLResponse(html, headers=headers.page_headers(style_text(), script_text(collector)))

    async def stats_json(request: Request) -> Response:
        if (response := await denied(request)) is not None:
            return response
        return JSONResponse(await run_in_threadpool(render_json, collector), headers=headers.data_headers())

    def endpoint_for(spec: EndpointSpec) -> Callable[[Request], Awaitable[Response]]:
        inner = spec.handler(collector)

        async def endpoint(request: Request) -> Response:
            if (response := await denied(request)) is not None:
                return response
            response = await inner(request)
            response.headers.update(headers.data_headers())
            return response

        return endpoint

    extra = [
        (p + spec.suffix, endpoint_for(spec), list(spec.methods)) for spec in parts.ENDPOINTS if spec.enabled(config)
    ]

    if dependencies:
        try:
            from fastapi import APIRouter
        except ImportError as exc:
            raise ImportError("stats_router(dependencies=...) needs FastAPI installed") from exc
        router = APIRouter(dependencies=list(dependencies), include_in_schema=False)
        router.add_api_route(p, stats_page, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route(p + ".json", stats_json, methods=["GET"])
        for route_path, endpoint, methods in extra:
            router.add_api_route(route_path, endpoint, methods=methods)
        return router
    return Router(
        routes=[
            Route(p, stats_page, methods=["GET"]),
            Route(p + ".json", stats_json, methods=["GET"]),
            *[Route(route_path, endpoint, methods=methods) for route_path, endpoint, methods in extra],
        ]
    )


def mount_stats(
    app: Any, collector: StatsCollector, path: str | None = None, dependencies: Sequence[Any] | None = None
) -> None:
    """Add the stats routes to a Starlette or FastAPI ``app``."""
    app.router.routes.extend(stats_router(collector, path, dependencies).routes)
