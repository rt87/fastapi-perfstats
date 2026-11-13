"""Payload sizes extension: aggregation in both stores, columns, formatting and the page table."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from fakes import make_call, make_route_stats
from fastapi_perfstats import StatsCollector, StatsConfig, render_html, render_json
from fastapi_perfstats.ext import sizes
from fastapi_perfstats.ext.sizes import _bytes
from fastapi_perfstats.models import RouteStats
from fastapi_perfstats.stores import MemoryStore, SqliteStore, StatsStore

KEY = "GET /items/{item_id}"


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StatsStore]:
    config = StatsConfig(sqlite_path=str(tmp_path / "s.db"))
    s: StatsStore = MemoryStore(config) if request.param == "memory" else SqliteStore(config)
    yield s
    s.close()


def only(store: StatsStore, key: str = KEY) -> RouteStats:
    return next(r for r in store.routes() if r.key == key)


# ---- aggregation -----------------------------------------------------------------------------------------
def test_means_and_max(store: StatsStore) -> None:
    store.record(make_call(request_bytes=100, response_bytes=1000))
    store.record(make_call(request_bytes=300, response_bytes=3000))
    row = only(store)
    assert row.mean_request_bytes == 200.0
    assert row.mean_response_bytes == 2000.0
    assert row.max_response_bytes == 3000


def test_max_keeps_the_largest_not_the_latest(store: StatsStore) -> None:
    store.record(make_call(request_bytes=1, response_bytes=5000))
    store.record(make_call(request_bytes=1, response_bytes=10))
    assert only(store).max_response_bytes == 5000


def test_unknown_sizes_stay_none(store: StatsStore) -> None:
    store.record(make_call())
    row = only(store)
    assert row.mean_request_bytes is None
    assert row.mean_response_bytes is None
    assert row.max_response_bytes is None


def test_calls_without_sizes_do_not_dilute_the_mean(store: StatsStore) -> None:
    store.record(make_call(request_bytes=100, response_bytes=400))
    store.record(make_call())
    store.record(make_call(request_bytes=300, response_bytes=800))
    row = only(store)
    assert row.count == 3
    assert row.mean_request_bytes == 200.0
    assert row.mean_response_bytes == 600.0


def test_zero_bytes_count_as_known(store: StatsStore) -> None:
    store.record(make_call(request_bytes=0, response_bytes=0))
    row = only(store)
    assert row.mean_request_bytes == 0.0
    assert row.max_response_bytes == 0


def test_head_style_call_with_zero_response_bytes(store: StatsStore) -> None:
    store.record(make_call(key="HEAD /home", request_bytes=0, response_bytes=0))
    assert only(store, "HEAD /home").max_response_bytes == 0


def test_partial_sizes_are_skipped(store: StatsStore) -> None:
    store.record(make_call(request_bytes=10, response_bytes=None))
    store.record(make_call(request_bytes=None, response_bytes=10))
    assert only(store).mean_request_bytes is None


def test_routes_are_independent(store: StatsStore) -> None:
    store.record(make_call(key="GET /a", request_bytes=1, response_bytes=10))
    store.record(make_call(key="GET /b", request_bytes=2, response_bytes=20))
    assert only(store, "GET /a").max_response_bytes == 10
    assert only(store, "GET /b").max_response_bytes == 20


def test_reset_clears_sizes(store: StatsStore) -> None:
    store.record(make_call(request_bytes=1, response_bytes=10))
    store.reset()
    store.record(make_call())
    assert only(store).max_response_bytes is None


def test_sqlite_sizes_survive_reopen(tmp_path: Path) -> None:
    config = StatsConfig(sqlite_path=str(tmp_path / "s.db"))
    first = SqliteStore(config)
    first.record(make_call(request_bytes=10, response_bytes=100))
    first.close()
    second = SqliteStore(config)
    try:
        assert only(second).mean_response_bytes == 100.0
    finally:
        second.close()


def test_sqlite_table_schema_is_idempotent() -> None:
    db = sqlite3.connect(":memory:")
    for _ in range(2):
        for statement in sizes.EXT.sqlite_schema():
            db.execute(statement)
    sizes.EXT.sqlite_update(db, make_call(request_bytes=1, response_bytes=2))
    sizes.EXT.sqlite_update(db, make_call())  # unknown sizes: no row change
    assert db.execute("SELECT sized_count, req_total, resp_total, resp_max FROM ext_sizes").fetchall() == [(1, 1, 2, 2)]
    sizes.EXT.sqlite_finish({}, db, StatsConfig(), 0.0)  # row for an unknown key is ignored
    db.close()


# ---- formatting and columns --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("n", "text"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024**2, "1.0 MiB"),
        (3 * 1024**3, "3.0 GiB"),
        (1024**5, "1024.0 TiB"),
    ],
)
def test_bytes_formatting(n: float, text: str) -> None:
    assert _bytes(n) == text


def test_columns_cells_and_none() -> None:
    cols = {c.header: c for c in sizes.EXT.columns(StatsConfig())}
    assert [(c.table, c.order) for c in cols.values()] == [("sizes", 30), ("sizes", 40), ("sizes", 50)]
    row = make_route_stats(mean_request_bytes=1536.0, mean_response_bytes=10.0, max_response_bytes=2048)
    config = StatsConfig()
    assert cols["Mean request"].cell(row, config) == ("1.5 KiB", 1536.0)
    assert cols["Mean response"].cell(row, config) == ("10 B", 10.0)
    assert cols["Max response"].cell(row, config) == ("2.0 KiB", 2048)
    empty = make_route_stats()
    for column in cols.values():
        assert column.cell(empty, config) == ("n/a", -1.0)


# ---- page and middleware -------------------------------------------------------------------------------------
def _app(collector: StatsCollector) -> Starlette:
    async def echo(request: Request) -> Response:
        body = await request.body()
        return JSONResponse({"size": len(body)})

    async def stream(request: Request) -> Response:
        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(3):
                yield b"x" * 100

        return StreamingResponse(chunks())

    app = Starlette(routes=[Route("/echo", echo, methods=["POST", "GET"]), Route("/stream", stream)])
    app.add_middleware(collector.middleware)
    return app


def test_page_shows_sizes_table_with_raw_values() -> None:
    collector = StatsCollector(StatsConfig())
    collector.record_call(make_call(request_bytes=1024, response_bytes=2048))
    html = render_html(collector)
    assert "<h2>Payload sizes</h2>" in html
    assert "1.0 KiB" in html and "2.0 KiB" in html
    assert 'data-v="2048"' in html


def test_page_shows_na_without_sizes() -> None:
    collector = StatsCollector(StatsConfig())
    collector.record_call(make_call())
    html = render_html(collector)
    assert html.count(">n/a<") >= 3


def test_json_carries_the_fields() -> None:
    collector = StatsCollector(StatsConfig())
    collector.record_call(make_call(request_bytes=10, response_bytes=30))
    route = render_json(collector)["routes"][0]
    assert route["mean_request_bytes"] == 10.0
    assert route["mean_response_bytes"] == 30.0
    assert route["max_response_bytes"] == 30


def test_post_body_and_json_response_sizes() -> None:
    collector = StatsCollector(StatsConfig())
    client = TestClient(_app(collector))
    response = client.post("/echo", content=b"a" * 1000)
    row = only(collector.store, "POST /echo")
    assert row.mean_request_bytes == 1000
    assert row.mean_response_bytes == len(response.content)


def test_streaming_response_is_summed_across_chunks() -> None:
    collector = StatsCollector(StatsConfig())
    TestClient(_app(collector)).get("/stream")
    assert only(collector.store, "GET /stream").max_response_bytes == 300


def test_chunked_request_body_is_summed() -> None:
    collector = StatsCollector(StatsConfig())

    def body() -> Iterator[bytes]:
        yield b"a" * 400
        yield b"b" * 600

    TestClient(_app(collector)).post("/echo", content=body())
    assert only(collector.store, "POST /echo").mean_request_bytes == 1000


def test_track_sizes_false_leaves_none() -> None:
    collector = StatsCollector(StatsConfig(track_sizes=False))
    TestClient(_app(collector)).post("/echo", content=b"a" * 1000)
    row = only(collector.store, "POST /echo")
    assert row.mean_request_bytes is None
    assert row.max_response_bytes is None
    assert ">n/a<" in render_html(collector)


def test_decorator_leaves_sizes_unknown() -> None:
    collector = StatsCollector(StatsConfig())

    @collector.track("op")
    async def op() -> dict[str, int]:
        return {"x": 1}

    async def run() -> None:
        await op()

    asyncio.run(run())
    assert only(collector.store, "op").mean_response_bytes is None
