"""Hook wiring: collector, middleware, route class and page call the stage-0 contract hooks."""

import asyncio
import logging
import re

import pytest
from fastapi import APIRouter, Depends, FastAPI
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from fakes import RecordingStore, make_call
from fastapi_perfstats import (
    SlowExtra,
    StatsCollector,
    StatsConfig,
    StatsStore,
    lifecycle,
    mount_stats,
    render_html,
    render_json,
    slow_context,
    stats_router,
)
from fastapi_perfstats import ext as ext_module
from fastapi_perfstats import headers as headers_module
from fastapi_perfstats import page as page_module
from fastapi_perfstats import parts as parts_module
from fastapi_perfstats.ext.base import Column, EndpointSpec, NoopExtension, PagePart
from fastapi_perfstats.middleware import StatsMiddleware
from fastapi_perfstats.stores import MemoryStore


def seeded(**config: object) -> StatsCollector:
    collector = StatsCollector(StatsConfig(slow_threshold_ms=0, **config))  # type: ignore[arg-type]
    collector.store.record(make_call("GET /items/{id}", 5, 200))
    collector.store.record(make_call("POST /items", 7, 500))
    return collector


class ColumnExtension(NoopExtension):
    def __init__(self, columns: tuple[Column, ...]) -> None:
        super().__init__("test_columns")
        self._columns = columns

    def columns(self, config: StatsConfig) -> tuple[Column, ...]:
        return self._columns


def headers_of(html: str) -> list[str]:
    return re.findall(r"<th>(.*?)</th>", html)


# -- columns -----------------------------------------------------------------------------------------------


def test_extension_column_appears_in_the_right_position(monkeypatch):
    column = Column("freq", "Extra", 45, lambda r, c: (f"n={r.count}", r.count))
    monkeypatch.setattr(ext_module, "EXTENSIONS", (ColumnExtension((column,)),))
    html = render_html(seeded())
    freq = html.split('id="freq"')[1].split("</table>")[0]
    assert headers_of(freq) == ["Method", "Route", "Count", "5xx", "Extra", "4xx", "Share", "Calls/min (all)"]
    assert '<td data-v="1">n=1</td>' in freq


def test_calls_per_min_header_without_recent_activity():
    html = render_html(seeded(recent_minutes=0))
    assert "<th>Calls/min</th>" in html and "(all)" not in html


def test_raw_cells_are_trusted_and_other_cells_escaped(monkeypatch):
    raw = Column("freq", "Spark", 25, lambda r, c: ("<svg></svg>", None), raw_html=True)
    escaped = Column("freq", "Text", 26, lambda r, c: ("<b>x</b>", None))
    monkeypatch.setattr(ext_module, "EXTENSIONS", (ColumnExtension((raw, escaped)),))
    html = render_html(seeded())
    assert "<td><svg></svg></td>" in html
    assert "<td>&lt;b&gt;x&lt;/b&gt;</td>" in html
    assert "<td><b>x</b></td>" not in html


def test_sizes_table_hidden_without_columns_and_shown_with(monkeypatch):
    monkeypatch.setattr(ext_module, "EXTENSIONS", ())
    assert 'id="sizes"' not in render_html(seeded())
    size = Column("sizes", "Mean in", 30, lambda r, c: ("12.0", 12.0))
    monkeypatch.setattr(ext_module, "EXTENSIONS", (ColumnExtension((size,)),))
    html = render_html(seeded())
    table = html.split('id="sizes"')[1].split("</table>")[0]
    assert headers_of(table) == ["Method", "Route", "Mean in"]
    assert "Payload sizes" in html


def test_core_cell_classes_survive():
    html = render_html(seeded())
    assert '<td data-v="1" class="bad">1</td>' in html  # POST /items has one 5xx
    assert 'class="bad">500</td>' in html


# -- page parts ---------------------------------------------------------------------------------------------


def test_page_parts_script_body_attrs_and_top_html(monkeypatch):
    part_a = PagePart("a", lambda c: "<p id='top-a'>A</p>", js="var a=1;", body_attrs=lambda cfg: {"data-a": "1"})
    part_b = PagePart("b", lambda c: "<p id='top-b'>B</p>", js="var b=2;", body_attrs=lambda cfg: {"data-b": '"q"'})
    monkeypatch.setattr(parts_module, "PAGE_PARTS", (part_a, part_b))
    collector = seeded()
    html = render_html(collector)
    assert '<body data-a="1" data-b="&quot;q&quot;">' in html
    assert html.index("top-a") < html.index("top-b") < html.index("<h2>Call frequency</h2>")
    assert html.index("</p>") < html.index("top-a")  # after the intro paragraph
    script = page_module.script_text(collector)
    assert script.endswith("var a=1;var b=2;")
    assert f"<script>{script}</script>" in html
    assert f"<style>{page_module.style_text()}</style>" in html


# -- endpoints, auth, headers --------------------------------------------------------------------------------


def make_endpoint(enabled: bool = True) -> EndpointSpec:
    def handler(collector: StatsCollector):
        async def run(request: Request) -> Response:
            return PlainTextResponse("hello")

        return run

    return EndpointSpec("/extra", ("GET",), lambda config: enabled, handler)


def test_endpoint_enabled_disabled_and_authorized(monkeypatch):
    monkeypatch.setattr(parts_module, "ENDPOINTS", (make_endpoint(True),))
    collector = StatsCollector(StatsConfig(basic_auth=("u", "p")))
    client = TestClient(Starlette(routes=stats_router(collector).routes))
    denied = client.get("/stats/extra")
    assert denied.status_code == 401
    ok = client.get("/stats/extra", auth=("u", "p"))
    assert (ok.status_code, ok.text) == (200, "hello")

    monkeypatch.setattr(parts_module, "ENDPOINTS", (make_endpoint(False),))
    off = TestClient(Starlette(routes=stats_router(StatsCollector(StatsConfig())).routes))
    assert off.get("/stats/extra").status_code == 404


def test_endpoint_on_fastapi_router_with_dependencies(monkeypatch):
    monkeypatch.setattr(parts_module, "ENDPOINTS", (make_endpoint(True),))
    app = FastAPI()
    calls = []

    def dep() -> None:
        calls.append(1)

    mount_stats(app, StatsCollector(StatsConfig()), dependencies=[Depends(dep)])
    response = TestClient(app).get("/stats/extra")
    assert (response.status_code, response.text) == (200, "hello")
    assert calls == [1]


def test_headers_hooks_are_applied(monkeypatch):
    seen = {}

    def page_headers(css: str, js: str) -> dict[str, str]:
        seen["css"], seen["js"] = css, js
        return {"X-Page": "1"}

    monkeypatch.setattr(headers_module, "page_headers", page_headers)
    monkeypatch.setattr(headers_module, "data_headers", lambda: {"X-Data": "1"})
    monkeypatch.setattr(parts_module, "ENDPOINTS", (make_endpoint(True),))
    collector = StatsCollector(StatsConfig(basic_auth=("u", "p")))
    client = TestClient(Starlette(routes=stats_router(collector).routes))
    page = client.get("/stats", auth=("u", "p"))
    assert page.headers["x-page"] == "1" and "x-data" not in page.headers
    assert seen == {"css": page_module.style_text(), "js": page_module.script_text(collector)}
    assert f"<script>{seen['js']}</script>" in page.text
    assert client.get("/stats.json", auth=("u", "p")).headers["x-data"] == "1"
    assert client.get("/stats/extra", auth=("u", "p")).headers["x-data"] == "1"
    unauthorized = client.get("/stats")
    assert unauthorized.status_code == 401 and unauthorized.headers["x-data"] == "1"


def test_authentication_warning_only_when_nothing_protects_the_page(caplog):
    caplog.set_level(logging.WARNING, logger="fastapi_perfstats")
    stats_router(StatsCollector(StatsConfig(access=lambda request: True)))
    assert caplog.text == ""
    stats_router(StatsCollector(StatsConfig()), dependencies=[Depends(lambda: None)])
    assert caplog.text == ""
    stats_router(StatsCollector(StatsConfig()))
    assert "WITHOUT authentication" in caplog.text


def test_json_has_dropped_and_slow_thresholds():
    collector = StatsCollector(StatsConfig(slow_thresholds={"GET /a": 5.0, "GET /b": None}))
    data = render_json(collector)
    assert data["dropped"] == 0
    assert data["slow_thresholds"] == {"GET /a": 5.0, "GET /b": None}


def test_slow_heading_uses_config_summary():
    assert "Slow calls (&gt;= 0 ms)" in render_html(seeded())
    assert "Slow calls (disabled)" in render_html(StatsCollector(StatsConfig(slow_threshold_ms=None)))


# -- collector.finish ---------------------------------------------------------------------------------------


SCOPE = {"type": "http", "method": "GET", "path": "/things/7"}


def test_finish_captures_slow_context_only_for_slow_calls(monkeypatch):
    captured = []

    def capture(scope, config, key, duration_ms):
        captured.append((key, duration_ms))
        return SlowExtra(query="a=1", request_id="r1", context={"k": "v"})

    monkeypatch.setattr(slow_context, "capture", capture)
    store = RecordingStore()
    collector = StatsCollector(StatsConfig(storage=store, slow_threshold_ms=100, unmatched="raw"))
    collector.finish(SCOPE, 10.0, 200)
    collector.finish(SCOPE, 150.0, 200, request_bytes=3, response_bytes=4)
    assert captured == [("GET /things/7", 150.0)]
    fast, slow = store.records
    assert (fast.query, fast.request_id, fast.context) == (None, None, None)
    assert (slow.query, slow.request_id, slow.context) == ("a=1", "r1", {"k": "v"})
    assert (slow.method, slow.path, slow.request_bytes, slow.response_bytes) == ("GET", "/things/7", 3, 4)
    assert slow.ts > 0


def test_finish_uses_per_key_threshold_hook(monkeypatch):
    monkeypatch.setattr(StatsConfig, "slow_threshold_for", lambda self, key: None, raising=True)
    captured = []

    def capture(*args):
        captured.append(args)
        return SlowExtra()

    monkeypatch.setattr(slow_context, "capture", capture)
    StatsCollector(StatsConfig(storage=RecordingStore(), slow_threshold_ms=0)).finish(SCOPE, 1.0, 200)
    assert captured == []


def test_finish_skips_when_key_func_returns_none_and_truncates():
    store = RecordingStore()
    skip = StatsCollector(StatsConfig(storage=store, key_func=lambda scope: None))
    skip.finish(SCOPE, 1.0, 200)
    assert store.records == []
    short = StatsCollector(StatsConfig(storage=store, max_key_length=6, unmatched="raw"))
    short.finish(SCOPE, 1.0, 200)
    assert store.records[0].key == "GET /t"


def test_reset_and_close_delegate_to_the_store():
    class Store(RecordingStore):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []

        def reset(self) -> None:
            self.events.append("reset")

        def close(self) -> None:
            self.events.append("close")

    store = Store()
    collector = StatsCollector(StatsConfig(storage=store))
    collector.reset()
    collector.close()
    assert store.events == ["reset", "close"]
    with pytest.raises(NotImplementedError):
        StatsCollector(StatsConfig(storage=RecordingStore())).reset()


def test_track_decorator_records_call_record():
    store = RecordingStore()
    collector = StatsCollector(StatsConfig(storage=store))

    @collector.track("job")
    async def job() -> str:
        return "x"

    async def run() -> object:
        return await job()

    assert asyncio.run(run()) == "x"
    (call,) = store.records
    assert (call.key, call.method, call.path, call.status) == ("job", "", "", 200)


# -- middleware / route class ---------------------------------------------------------------------------------


def test_middleware_delegates_lifespan_and_passes_other_scopes(monkeypatch):
    seen = []

    async def handle(collector, app, scope, receive, send):
        seen.append(scope["type"])

    monkeypatch.setattr(lifecycle, "handle_lifespan", handle)
    passed = []

    async def app(scope, receive, send):
        passed.append(scope["type"])

    middleware = StatsMiddleware(app, StatsCollector(StatsConfig()))
    asyncio.run(middleware({"type": "lifespan"}, None, None))  # type: ignore[arg-type]
    asyncio.run(middleware({"type": "websocket"}, None, None))  # type: ignore[arg-type]
    assert seen == ["lifespan"] and passed == ["websocket"]


def test_middleware_and_route_class_record_through_finish():
    store = MemoryStore(StatsConfig())
    collector = StatsCollector(StatsConfig(storage=store))

    async def hello(request: Request) -> Response:
        return JSONResponse({})

    app = Starlette(routes=[Route("/hello", hello)])
    app.add_middleware(collector.middleware)
    assert TestClient(app).get("/hello").status_code == 200

    router = APIRouter(route_class=collector.route_class)

    @router.get("/r/{n}")
    async def route(n: int) -> dict[str, int]:
        return {"n": n}

    api = FastAPI()
    api.include_router(router)
    TestClient(api).get("/r/1")
    keys = {r.key for r in collector.store.routes()}
    assert keys == {"GET /hello", "GET /r/{n}"}
    assert isinstance(collector.store, StatsStore)
