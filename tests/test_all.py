import asyncio
import base64
import logging
import queue
import secrets
import threading
import time
from collections.abc import MutableMapping
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.types import ASGIApp

from fakes import make_call
from fastapi_stats import (
    MemoryStore,
    SqliteStore,
    StatsCollector,
    StatsConfig,
    StatsStore,
    mount_stats,
    render_html,
)
from fastapi_stats.models import OTHER_KEY, nearest_rank
from fastapi_stats.stores import build_store


def call_asgi(app: ASGIApp, scope: MutableMapping[str, Any]) -> None:
    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request"}

    async def send(message: MutableMapping[str, Any]) -> None:
        return None

    async def run() -> None:
        await app(scope, receive, send)

    asyncio.run(run())


def make_app(cfg: StatsConfig | None = None, **kw):
    collector = StatsCollector(cfg or StatsConfig(**kw))
    app = FastAPI()
    app.add_middleware(collector.middleware)
    mount_stats(app, collector)

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("x")

    @app.get("/bad")
    async def bad():
        raise HTTPException(500, "x")

    @app.get("/missing")
    async def missing():
        raise HTTPException(404, "x")

    @app.get("/health")
    async def health():
        return "ok"

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(0.03)
        return 1

    return app, collector


def by_key(collector):
    return {r.key: r for r in collector.store.routes()}


def test_templated_route_is_one_key_and_stats():
    app, c = make_app()
    cl = TestClient(app)
    for i in range(5):
        assert cl.get(f"/items/{i}").status_code == 200
    r = by_key(c)["GET /items/{item_id}"]
    assert r.count == 5 and r.errors_5xx == 0 and r.min_ms <= r.mean_ms <= r.max_ms
    assert set(r.quantiles) == {"p50", "p95", "p99"}
    assert len(by_key(c)) == 1


def test_errors_counted_separately():
    app, c = make_app()
    cl = TestClient(app, raise_server_exceptions=False)
    cl.get("/boom")
    cl.get("/bad")
    cl.get("/missing")
    cl.get("/items/abc")  # 422
    k = by_key(c)
    assert k["GET /boom"].errors_5xx == 1
    assert k["GET /bad"].errors_5xx == 1
    assert k["GET /missing"].errors_4xx == 1 and k["GET /missing"].errors_5xx == 0
    assert k["GET /items/{item_id}"].errors_4xx == 1


def test_exception_propagates_when_not_suppressed():
    app, _ = make_app()
    with pytest.raises(RuntimeError):
        TestClient(app).get("/boom")


def test_excluded_paths_and_stats_page_not_counted():
    app, c = make_app()
    cl = TestClient(app)
    cl.get("/health")
    cl.get("/stats")
    cl.get("/stats.json")
    assert by_key(c) == {}


def test_unmatched_path_uses_raw_path_and_cap_goes_to_other():
    app, c = make_app(max_routes=3, unmatched="raw")
    cl = TestClient(app)
    for i in range(10):
        assert cl.get(f"/nope/{i}").status_code == 404
    keys = by_key(c)
    assert len(keys) == 4 and OTHER_KEY in keys
    assert keys[OTHER_KEY].count == 10 - 3


def test_quantile_override_replaces_defaults():
    app, c = make_app(quantiles=(0.9,))
    TestClient(app).get("/items/1")
    assert set(by_key(c)["GET /items/{item_id}"].quantiles) == {"p90"}
    assert "p95" not in render_html(c)


@pytest.mark.parametrize("bad", [(), (0,), (1.5,)])
def test_invalid_quantiles(bad):
    with pytest.raises(ValueError):
        StatsConfig(quantiles=bad)


@pytest.mark.parametrize(
    "kw",
    [{"window_size": 0}, {"slow_threshold_ms": -1}, {"stats_path": "stats"}, {"max_routes": 0}],
)
def test_invalid_config(kw):
    with pytest.raises(ValueError):
        StatsConfig(**kw)


def test_nearest_rank():
    data = [1.0, 2.0, 3.0, 4.0]
    assert nearest_rank(data, 0.5) == 2.0
    assert nearest_rank(data, 0.95) == 4.0
    assert nearest_rank(data, 1) == 4.0
    assert nearest_rank([], 0.5) == 0.0


def test_slow_threshold():
    app, c = make_app(slow_threshold_ms=20)
    cl = TestClient(app)
    cl.get("/items/1")
    cl.get("/slow")
    slow = c.store.slow_calls()
    assert [s.key for s in slow] == ["GET /slow"] and slow[0].duration_ms >= 20 and slow[0].status == 200


def test_slow_disabled_and_zero_threshold():
    app, c = make_app(slow_threshold_ms=None)
    TestClient(app).get("/slow")
    assert c.store.slow_calls() == []
    app, c = make_app(slow_threshold_ms=0)
    TestClient(app).get("/items/1")
    assert len(c.store.slow_calls()) == 1


def test_key_func_and_skip():
    cfg = StatsConfig(key_func=lambda s: None if s["path"].startswith("/items/1") else "custom")
    app, c = make_app(cfg)
    cl = TestClient(app)
    cl.get("/items/1")
    cl.get("/items/2")
    assert list(by_key(c)) == ["custom"]


def test_key_truncated():
    app, c = make_app(max_key_length=10)
    TestClient(app).get("/items/1")
    assert list(by_key(c)) == ["GET /items"]


def test_non_http_scope_passthrough():
    c = StatsCollector()
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    mw = c.middleware(inner)
    call_asgi(mw, {"type": "lifespan"})
    assert seen == ["lifespan"] and c.store.routes() == []


def test_exception_before_response_start_is_500():
    c = StatsCollector(StatsConfig(unmatched="raw"))

    async def inner(scope, receive, send):
        raise ValueError

    mw = c.middleware(inner)
    with pytest.raises(ValueError):
        call_asgi(mw, {"type": "http", "path": "/x", "method": "GET"})
    assert by_key(c)["GET /x"].errors_5xx == 1


# ---- per route -------------------------------------------------------------------------------------------
def test_route_class_wraps_only_its_router():
    c = StatsCollector()
    app = FastAPI()
    router = APIRouter(route_class=c.route_class)

    @router.get("/r/{x}")
    async def r(x: int):
        if x == 0:
            raise HTTPException(503, "no")
        return x

    @router.get("/health")
    async def h():
        return 1

    @router.get("/raw")
    async def raw():
        raise RuntimeError

    @app.get("/untracked")
    async def u():
        return 1

    app.include_router(router)
    cl = TestClient(app, raise_server_exceptions=False)
    cl.get("/r/1")
    cl.get("/r/0")
    cl.get("/untracked")
    cl.get("/health")
    cl.get("/raw")
    k = by_key(c)
    assert k["GET /r/{x}"].count == 2 and k["GET /r/{x}"].errors_5xx == 1
    assert k["GET /raw"].errors_5xx == 1
    assert "GET /untracked" not in k and "GET /health" not in k


def test_track_decorator():
    c = StatsCollector()
    app = FastAPI()

    @app.get("/d")
    @c.track("dec")
    async def d(fail: bool = False):
        if fail:
            raise HTTPException(400, "x")
        return {"ok": 1}

    @app.get("/e")
    @c.track()
    async def e():
        raise RuntimeError

    cl = TestClient(app, raise_server_exceptions=False)
    cl.get("/d")
    cl.get("/d?fail=true")
    cl.get("/e")
    k = by_key(c)
    assert k["dec"].count == 2 and k["dec"].errors_4xx == 1
    assert k["e"].errors_5xx == 1


# ---- stores ---------------------------------------------------------------------------------------------
@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    def build(**kw):
        cfg = StatsConfig(storage=request.param, sqlite_path=str(tmp_path / "s.db"), **kw)
        s = build_store(cfg)
        request.addfinalizer(s.close)
        return s

    return build


def test_store_basic_stats(store):
    s = store(slow_threshold_ms=50)
    for ms in (10, 20, 30, 40, 100):
        s.record(make_call("GET /a", ms, 200))
    s.record(make_call("GET /a", 5, 404))
    s.record(make_call("GET /a", 5, 500))
    (r,) = s.routes()
    assert (r.count, r.errors_4xx, r.errors_5xx, r.min_ms, r.max_ms) == (7, 1, 1, 5, 100)
    assert r.mean_ms == pytest.approx(210 / 7)
    assert r.share == 1.0 and r.rate_per_min > 0
    assert len(s.slow_calls()) == 1


def test_store_window_limit(store):
    s = store(window_size=3)
    for ms in (1000, 1, 2, 3):
        s.record(make_call("k", ms, 200))
    (r,) = s.routes()
    assert r.count == 4 and r.max_ms == 1000  # all-time aggregates survive
    assert r.quantiles["p99"] == 3  # but quantiles only see the last 3 durations


def test_store_route_cap(store):
    s = store(max_routes=2)
    for k in "abcde":
        s.record(make_call(k, 1, 200))
    keys = {r.key: r.count for r in s.routes()}
    assert keys == {"a": 1, "b": 1, OTHER_KEY: 3}


def test_store_slow_log_bounded_most_recent_kept(store):
    s = store(slow_threshold_ms=1, max_slow_calls=3)
    for i in range(10):
        s.record(make_call("k", 10 + i, 200, ts=1000 + i))
    slow = s.slow_calls()
    assert [c.duration_ms for c in slow] == [19, 18, 17]  # newest first


def test_sqlite_prunes_rows_and_persists(tmp_path):
    cfg = StatsConfig(
        storage="sqlite",
        sqlite_path=str(tmp_path / "p.db"),
        window_size=5,
        max_slow_calls=4,
        max_routes=2,
        slow_threshold_ms=0,
    )
    s = SqliteStore(cfg)
    for i in range(50):
        s.record(make_call(f"k{i % 4}", 1.0, 200))
    counts = s.row_counts()
    assert counts["routes"] == 3 and counts["durations"] <= 5 * 3 and counts["slow_calls"] == 4
    s.close()
    s2 = SqliteStore(cfg)  # persistent: data survives restart
    assert sum(r.count for r in s2.routes()) == 50
    s2.close()


def test_sqlite_queue_full_drops(tmp_path):
    s = SqliteStore(StatsConfig(storage="sqlite", sqlite_path=str(tmp_path / "q.db")))
    s.flush()
    full: queue.Queue[object] = queue.Queue(1)
    full.put("filler")
    real_q, s._q = s._q, full  # writer keeps draining the real queue; records go to the full one
    s.record(make_call("k", 1, 200))
    assert s.dropped == 1
    s._q = real_q
    s.close()


def test_sqlite_flush_after_close_and_write_error(tmp_path):
    s = SqliteStore(StatsConfig(storage="sqlite", sqlite_path=str(tmp_path / "e.db")))
    s._apply([])  # empty batch no-op
    s._db.execute("DROP TABLE durations")  # force sqlite error in writer -> rolled back, thread survives
    s.record(make_call("k", 1, 200))
    s.flush()
    assert s._thread.is_alive()
    s.close()
    s.flush()  # no-op once the thread is gone


def test_build_store_variants(tmp_path):
    assert isinstance(build_store(StatsConfig()), MemoryStore)
    custom = MemoryStore(StatsConfig())
    assert build_store(StatsConfig(storage=custom)) is custom
    with pytest.raises(ValueError):
        build_store(StatsConfig(storage="redis"))
    assert isinstance(custom, StatsStore)


def test_memory_store_thread_safety():
    s = MemoryStore(StatsConfig())

    def work() -> None:
        for _ in range(500):
            s.record(make_call("k", 1, 200))

    ts = [threading.Thread(target=work) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert s.routes()[0].count == 4000


def test_sqlite_through_app(tmp_path):
    app, c = make_app(storage="sqlite", sqlite_path=str(tmp_path / "a.db"))
    TestClient(app).get("/items/1")
    assert by_key(c)["GET /items/{item_id}"].count == 1
    c.store.close()


# ---- page / json -----------------------------------------------------------------------------------------
def test_html_escapes_malicious_key():
    app, c = make_app()
    evil = "/<script>alert(1)</script>\"'"
    TestClient(app).get(evil)
    c.store.record(make_call("GET <img src=x onerror=alert(1)>", 1, 200))
    html = TestClient(app).get("/stats").text
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert html.count("<script>") == 1  # only our own inline script


def test_html_and_json_endpoints():
    app, _ = make_app(quantiles=(0.5, 0.9), slow_threshold_ms=0)
    cl = TestClient(app)
    cl.get("/items/1")
    r = cl.get("/stats")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "Call frequency" in r.text and "Longest runtimes" in r.text and "p90" in r.text
    j = cl.get("/stats.json").json()
    assert j["quantiles"] == ["p50", "p90"] and j["routes"][0]["key"] == "GET /items/{item_id}"
    assert j["slow_calls"][0]["status"] == 200


def test_html_slow_disabled_label():
    app, _ = make_app(slow_threshold_ms=None)
    assert "disabled" in TestClient(app).get("/stats").text


def test_custom_stats_path():
    c = StatsCollector(StatsConfig(stats_path="/_internal/stats"))
    app = FastAPI()
    app.add_middleware(c.middleware)
    mount_stats(app, c)
    cl = TestClient(app)
    assert cl.get("/_internal/stats").status_code == 200
    assert cl.get("/_internal/stats.json").status_code == 200
    assert c.store.routes() == []  # custom path auto-excluded
    assert cl.get("/stats").status_code == 404
    # path override at mount time
    app2 = FastAPI()
    mount_stats(app2, c, path="/other/")
    assert TestClient(app2).get("/other").status_code == 200


def test_extra_dependencies_protect_page():
    c = StatsCollector()
    app = FastAPI()

    def deny():
        raise HTTPException(403, "no")

    mount_stats(app, c, dependencies=[Depends(deny)])
    cl = TestClient(app)
    assert cl.get("/stats").status_code == 403
    assert cl.get("/stats.json").status_code == 403


# ---- basic auth -------------------------------------------------------------------------------------------
def hdr(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def auth_client(caplog):
    caplog.set_level(logging.WARNING, logger="fastapi_stats")
    app, _ = make_app(basic_auth=("admin", "s3cret:with:colons"))
    return TestClient(app)


@pytest.mark.parametrize("path", ["/stats", "/stats.json"])
def test_basic_auth_correct(auth_client, path):
    assert auth_client.get(path, headers=hdr("admin", "s3cret:with:colons")).status_code == 200


@pytest.mark.parametrize("path", ["/stats", "/stats.json"])
@pytest.mark.parametrize(
    "headers",
    [
        {},
        hdr("admin", "wrong"),
        hdr("other", "s3cret:with:colons"),
        hdr("", ""),
        {"Authorization": "Bearer abc"},
        {"Authorization": "Basic !!!notbase64"},
        {"Authorization": "Basic " + base64.b64encode(b"nocolon").decode()},
        {"Authorization": "Basic"},
    ],
)
def test_basic_auth_rejected(auth_client, path, headers):
    r = auth_client.get(path, headers=headers)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")


def test_basic_auth_uses_compare_digest_for_both_fields(monkeypatch):
    calls: list[tuple[bytes, bytes]] = []
    real = secrets.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(secrets, "compare_digest", spy)  # page.py calls secrets.compare_digest at request time
    app, _ = make_app(basic_auth=("u", "p"))
    TestClient(app).get("/stats", headers=hdr("nope", "nope"))  # wrong user: password still compared
    assert [c[1] for c in calls] == [b"u", b"p"]


def test_basic_auth_password_not_in_repr_or_logs(caplog):
    cfg = StatsConfig(basic_auth=("u", "topsecret"))
    assert "topsecret" not in repr(cfg)
    caplog.set_level(logging.DEBUG)
    app, _ = make_app(cfg)
    TestClient(app).get("/stats", headers=hdr("u", "wrong-attempt"))
    assert "topsecret" not in caplog.text and "wrong-attempt" not in caplog.text


def test_warning_logged_without_auth_and_not_with(caplog):
    caplog.set_level(logging.WARNING, logger="fastapi_stats")
    make_app()
    assert "WITHOUT authentication" in caplog.text
    caplog.clear()
    make_app(basic_auth=("a", "b"))
    assert caplog.text == ""


def test_overhead_is_small():
    app, c = make_app()
    cl = TestClient(app)
    t = time.perf_counter()
    for _ in range(50):
        cl.get("/items/1")
    assert by_key(c)["GET /items/{item_id}"].count == 50
    assert time.perf_counter() - t < 10
