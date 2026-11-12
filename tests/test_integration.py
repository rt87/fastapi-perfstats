"""Cross-feature end-to-end tests (#24): real Starlette apps driven through ``TestClient``, no mocks.

Every stage-2 feature is merged, so nothing is skipped; each test combines several features the way an app would.
"""

import base64
import dataclasses
import hashlib
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from prometheus_client.parser import text_string_to_metric_families
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from fastapi_stats import StatsCollector, StatsConfig, mount_stats, render_json
from fastapi_stats.ext import EXTENSIONS
from fastapi_stats.ext.base import NoopExtension

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:s3cret").decode()}
GUARD = {"X-Stats-Reset": "1"}


async def echo(request: Request) -> Response:
    body = await request.body()
    return JSONResponse({"size": len(body)})


async def slow(request: Request) -> Response:
    time.sleep(0.03)
    return JSONResponse({"slow": True})


async def boom(request: Request) -> Response:
    return JSONResponse({"error": "x"}, status_code=500)


def build_app(collector: StatsCollector, *, stats_path: str | None = None) -> Starlette:
    inner = Starlette(routes=[Route("/items/{item_id}", echo, methods=["GET", "POST"])])
    app = Starlette(
        routes=[
            Route("/items/{item_id}", echo, methods=["GET", "POST"]),
            Route("/slow", slow),
            Route("/boom", boom),
            Mount("/v1", app=inner),
            Mount("/v2", routes=[Route("/items/{item_id}", echo)]),
        ]
    )
    app.add_middleware(collector.middleware)
    mount_stats(app, collector, path=stats_path)
    return app


@pytest.fixture(params=["memory", "sqlite"])
def config_for(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    def make(**fields: Any) -> StatsConfig:
        if request.param == "sqlite":
            fields.update(storage="sqlite", sqlite_path=str(tmp_path / "int.db"))
        return StatsConfig(**fields)

    return make


@pytest.fixture
def collector_of(config_for: Any) -> Iterator[Any]:
    made: list[StatsCollector] = []

    def make(**fields: Any) -> StatsCollector:
        collector = StatsCollector(config_for(**fields))
        made.append(collector)
        return collector

    yield make
    for collector in made:
        collector.close()


def keys_of(collector: StatsCollector) -> set[str]:
    collector.store.flush()
    return {r.key for r in collector.store.routes()}


def route_row(collector: StatsCollector, key: str) -> Any:
    collector.store.flush()
    return next(r for r in collector.store.routes() if r.key == key)


def prom(text: str) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            out.setdefault(sample.name, []).append(sample)
    return out


# ---- the wiring itself ---------------------------------------------------------------------------------------
def test_every_extension_is_real() -> None:
    assert {e.name for e in EXTENSIONS} >= {"stddev", "sizes", "recent"}
    assert not [e for e in EXTENSIONS if type(e) is NoopExtension]


# ---- #6 sizes -> #14 table and JSON ------------------------------------------------------------------------------
def test_sizes_flow_from_middleware_to_table_and_json(collector_of: Any) -> None:
    collector = collector_of()
    client = TestClient(build_app(collector))
    client.post("/items/1", content=b"a" * 2048)
    client.post("/items/1", content=b"a" * 1024)
    collector.store.flush()
    row = route_row(collector, "POST /items/{item_id}")
    assert row.mean_request_bytes == 1536.0
    assert row.mean_response_bytes == len(b'{"size":2048}') / 2 + len(b'{"size":1024}') / 2
    html = client.get("/stats", headers=AUTH).text
    assert "<h2>Payload sizes</h2>" in html
    assert "1.5 KiB" in html
    data = client.get("/stats.json").json()
    post = next(r for r in data["routes"] if r["key"] == "POST /items/{item_id}")
    assert post["mean_request_bytes"] == 1536.0 and post["max_response_bytes"] == len(b'{"size":2048}')


def test_sizes_off_shows_na(collector_of: Any) -> None:
    collector = collector_of(track_sizes=False)
    client = TestClient(build_app(collector))
    client.post("/items/1", content=b"abc")
    collector.store.flush()
    assert route_row(collector, "POST /items/{item_id}").mean_request_bytes is None
    assert client.get("/stats").text.count(">n/a<") >= 3


def test_head_requests_add_no_response_bytes(collector_of: Any) -> None:
    collector = collector_of()
    client = TestClient(build_app(collector))
    client.get("/items/1")
    client.head("/items/1")
    assert route_row(collector, "HEAD /items/{item_id}").max_response_bytes == 0


# ---- #12 threshold + #13 context -> slow table and JSON ------------------------------------------------------
def test_per_route_threshold_and_slow_context(collector_of: Any) -> None:
    collector = collector_of(
        slow_threshold_ms=60_000,
        slow_thresholds={"/slow": 10.0},
        slow_capture_query=True,
        slow_context=lambda scope: {"tenant": "acme"},
    )
    client = TestClient(build_app(collector))
    client.get("/slow?q=shoes&token=abc", headers={"X-Request-Id": "req-7"})
    client.get("/items/1")  # far below the global threshold
    collector.store.flush()
    data = render_json(collector)
    (call,) = data["slow_calls"]
    assert call["key"] == "GET /slow"
    assert call["query"] == "q=shoes&token=%5Bredacted%5D"
    assert call["request_id"] == "req-7"
    assert call["context"] == {"tenant": "acme"}
    assert data["slow_thresholds"] == {"/slow": 10.0}
    html = client.get("/stats").text
    assert "1 route override" in html
    assert "req-7" in html and "acme" in html


# ---- #10 reset clears core data and every extension ----------------------------------------------------------
def test_reset_clears_core_and_extension_data(collector_of: Any, config_for: Any) -> None:
    collector = collector_of(allow_reset=True, slow_thresholds={"/slow": 1.0})
    client = TestClient(build_app(collector))
    client.post("/items/1", content=b"x" * 100)
    client.get("/slow")
    client.get("/boom")
    collector.store.flush()
    assert route_row(collector, "GET /boom").errors_5xx == 1
    assert client.post("/stats/reset").status_code == 400  # CSRF guard
    assert client.post("/stats/reset", headers=GUARD).status_code == 204
    collector.store.flush()
    assert collector.store.routes() == [] and collector.store.slow_calls() == []
    path = collector.config.sqlite_path
    if collector.config.storage == "sqlite":
        db = sqlite3.connect(path)
        try:
            for table in ("routes", "durations", "slow_calls", "ext_sizes", "ext_stddev", "ext_recent"):
                assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,), table  # noqa: S608
        finally:
            db.close()
    client.get("/items/2")  # recording resumes after the reset
    assert keys_of(collector) == {"GET /items/{item_id}"}
    row = route_row(collector, "GET /items/{item_id}")
    assert row.count == 1 and row.mean_response_bytes is not None and sum(row.series) == 1


# ---- #8 CSP hashes with all parts enabled ----------------------------------------------------------------------
def test_csp_hashes_match_the_served_page_with_all_parts(collector_of: Any) -> None:
    collector = collector_of(allow_reset=True, refresh_seconds=5, prometheus=True)
    client = TestClient(build_app(collector))
    client.post("/items/1", content=b"hello")
    response = client.get("/stats")
    html = response.text
    assert 'id="stats-reset"' in html and 'id="refresh"' in html
    style = re.search(r"<style>(.*?)</style>", html, re.S)
    script = re.search(r"<script>(.*?)</script>", html, re.S)
    assert style and script

    def digest(text: str) -> str:
        return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"

    policy = response.headers["content-security-policy"]
    assert f"style-src {digest(style.group(1))};" in policy
    assert f"script-src {digest(script.group(1))};" in policy
    assert "default-src 'none'" in policy
    assert " onclick=" not in html and " style=" not in html
    assert response.headers["x-content-type-options"] == "nosniff"


# ---- #17 Prometheus + #9 auth ---------------------------------------------------------------------------------
def test_prometheus_after_load_with_auth_and_dropped_counter(collector_of: Any) -> None:
    collector = collector_of(prometheus=True, basic_auth=("admin", "s3cret"), access=lambda request: True)
    client = TestClient(build_app(collector))
    for _ in range(3):
        client.get("/items/1")
    client.get("/boom")
    collector.store.flush()
    assert client.get("/stats/metrics").status_code == 401
    response = client.get("/stats/metrics", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    samples = prom(response.text)
    by_route = {s.labels["route"]: s.value for s in samples["fastapi_stats_requests_total"]}
    assert by_route["/items/{item_id}"] == 3 and by_route["/boom"] == 1
    errors = {
        (s.labels["route"], s.labels["status_class"]): s.value for s in samples["fastapi_stats_responses_errors_total"]
    }
    assert errors[("/boom", "5xx")] == 1 and errors[("/items/{item_id}", "5xx")] == 0
    (dropped,) = samples["fastapi_stats_dropped_records_total"]
    assert dropped.value == 0
    assert "fastapi_stats_request_duration_seconds_count" in samples


def test_prometheus_denied_by_access_hook(collector_of: Any) -> None:
    collector = collector_of(prometheus=True, access=lambda request: False)
    client = TestClient(build_app(collector))
    assert client.get("/stats/metrics").status_code == 403
    assert client.get("/stats").status_code == 403


# ---- #5 unmatched grouping + #4 mount prefixes -------------------------------------------------------------------
def test_unmatched_grouping_and_mount_prefixes_together(collector_of: Any) -> None:
    collector = collector_of()
    client = TestClient(build_app(collector))
    client.get("/v1/items/1")
    client.get("/v2/items/1")
    client.get("/items/1")
    for i in range(5):
        client.get(f"/scanner/{i}.php")
    client.request("BREW", "/items/1")
    assert keys_of(collector) == {
        "GET /v1/items/{item_id}",
        "GET /v2/items/{item_id}",
        "GET /items/{item_id}",
        "GET <unmatched>",
        "OTHER /items/{item_id}",
    }
    assert route_row(collector, "GET <unmatched>").count == 5
    assert route_row(collector, "GET <unmatched>").errors_4xx == 5


def test_unmatched_skip_and_raw_modes(collector_of: Any) -> None:
    skip = collector_of(unmatched="skip")
    TestClient(build_app(skip)).get("/nope")
    assert keys_of(skip) == set()
    raw = collector_of(unmatched="raw")
    TestClient(build_app(raw)).get("/nope")
    assert keys_of(raw) == {"GET /nope"}


# ---- #7 lifespan close persists everything including extension tables ----------------------------------------------
def test_lifespan_close_persists_core_and_extension_tables(tmp_path: Path) -> None:
    path = tmp_path / "life.db"
    collector = StatsCollector(StatsConfig(storage="sqlite", sqlite_path=str(path), slow_thresholds={"/slow": 1.0}))
    with TestClient(build_app(collector)) as client:
        client.post("/items/1", content=b"z" * 10)
        client.get("/slow")
        client.get("/boom")
    db = sqlite3.connect(path)
    try:
        assert {r[0] for r in db.execute("SELECT key FROM routes")} == {
            "POST /items/{item_id}",
            "GET /slow",
            "GET /boom",
        }
        assert db.execute("SELECT COUNT(*) FROM slow_calls").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM ext_sizes").fetchone()[0] >= 1
        assert db.execute("SELECT COUNT(*) FROM ext_stddev").fetchone()[0] >= 1
        assert db.execute("SELECT COUNT(*) FROM ext_recent").fetchone()[0] >= 1
    finally:
        db.close()
    reopened = StatsCollector(StatsConfig(storage="sqlite", sqlite_path=str(path)))
    try:
        row = next(r for r in reopened.store.routes() if r.key == "POST /items/{item_id}")
        assert row.count == 1 and row.mean_request_bytes == 10.0 and sum(row.series) == 1
    finally:
        reopened.close()


def test_close_on_shutdown_false_leaves_the_store_open(tmp_path: Path) -> None:
    collector = StatsCollector(
        StatsConfig(storage="sqlite", sqlite_path=str(tmp_path / "open.db"), close_on_shutdown=False)
    )
    try:
        with TestClient(build_app(collector)) as client:
            client.get("/items/1")
        collector.store.reset()  # still usable: a closed store raises RuntimeError
    finally:
        collector.close()


# ---- the router mounted elsewhere ------------------------------------------------------------------------------------
def test_stats_router_at_a_custom_path_under_a_mount(collector_of: Any) -> None:
    # exclusion works on the raw path, so a router under a mount needs the prefixed path in exclude_paths (README)
    collector = collector_of(
        allow_reset=True, prometheus=True, stats_path="/admin/stats", exclude_paths=("/api/admin/stats",)
    )
    inner = Starlette()
    mount_stats(inner, collector, path="/admin/stats")
    app = Starlette(routes=[Mount("/api", app=inner), Route("/items/{item_id}", echo)])
    app.add_middleware(collector.middleware)
    client = TestClient(app)
    client.get("/items/1")
    assert client.get("/api/admin/stats").status_code == 200
    assert client.get("/api/admin/stats/metrics").status_code == 200
    assert client.post("/api/admin/stats/reset", headers=GUARD).status_code == 204
    assert collector.store.routes() == []


# ---- docs ----------------------------------------------------------------------------------------------------------
def test_every_config_field_is_in_the_readme_table() -> None:
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `(\w+)` \|", readme, re.M))
    fields = {f.name for f in dataclasses.fields(StatsConfig)}
    assert fields <= documented, sorted(fields - documented)


def test_readme_has_no_planned_markers() -> None:
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert "(planned" not in readme
