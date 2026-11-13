"""Key building: unmatched grouping and unknown methods (#5)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_perfstats import StatsCollector, StatsConfig, mount_stats, render_html
from fastapi_perfstats.keys import build_key
from fastapi_perfstats.models import OTHER_KEY, OTHER_METHOD, UNMATCHED_ROUTE


def make_app(**kw):
    collector = StatsCollector(StatsConfig(**kw))
    app = FastAPI()
    app.add_middleware(collector.middleware)
    mount_stats(app, collector)

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    return app, collector


def counts(collector):
    return {r.key: r.count for r in collector.store.routes()}


def test_default_groups_unknown_paths_into_one_key():
    app, c = make_app()
    cl = TestClient(app)
    for i in range(50):
        assert cl.get(f"/wp-login/{i}.php").status_code == 404
    assert counts(c) == {"GET <unmatched>": 50}


def test_flood_does_not_starve_real_routes():
    app, c = make_app(max_routes=2)
    cl = TestClient(app)
    for i in range(50):
        cl.get(f"/scan/{i}")
    cl.get("/items/1")
    keys = counts(c)
    assert keys["GET /items/{item_id}"] == 1
    assert OTHER_KEY not in keys


def test_raw_keeps_raw_path():
    app, c = make_app(unmatched="raw")
    TestClient(app).get("/nope/1")
    assert counts(c) == {"GET /nope/1": 1}


def test_skip_records_nothing_for_unmatched_but_still_matched():
    app, c = make_app(unmatched="skip")
    cl = TestClient(app)
    cl.get("/nope/1")
    assert counts(c) == {}
    cl.get("/items/1")
    assert counts(c) == {"GET /items/{item_id}": 1}


def test_unknown_method_on_existing_route_is_other_with_405():
    app, c = make_app()
    resp = TestClient(app).request("PROPFIND", "/items/1")
    assert resp.status_code == 405
    assert counts(c) == {"OTHER /items/{item_id}": 1}


def test_known_method_mismatch_keeps_template():
    app, c = make_app()
    assert TestClient(app).post("/items/1").status_code == 405
    assert counts(c) == {"POST /items/{item_id}": 1}


def test_unknown_method_on_unknown_path_groups_under_other():
    app, c = make_app()
    TestClient(app).request("FOO", "/x/1")
    assert counts(c) == {f"{OTHER_METHOD} {UNMATCHED_ROUTE}": 1}


def test_invalid_unmatched_value_raises():
    with pytest.raises(ValueError):
        StatsConfig(unmatched="nope")  # type: ignore[arg-type]


def test_html_escapes_unmatched_key():
    app, c = make_app()
    TestClient(app).get("/nope")
    html = render_html(c)
    assert "&lt;unmatched&gt;" in html
    assert "<unmatched>" not in html


def test_build_key_defaults_to_get_without_method():
    assert build_key({"path": "/a"}, StatsConfig(), "") == "GET <unmatched>"


def test_key_func_bypasses_unmatched_policy():
    cfg = StatsConfig(unmatched="skip", key_func=lambda scope: "custom")
    assert build_key({"method": "GET", "path": "/a"}, cfg, "") == "custom"
    cfg_none = StatsConfig(key_func=lambda scope: None)
    assert build_key({"method": "GET", "path": "/a"}, cfg_none, "") is None


def test_max_key_length_truncates():
    cfg = StatsConfig(unmatched="raw", max_key_length=10)
    assert build_key({"method": "GET", "path": "/" + "a" * 50}, cfg, "") == "GET /aaaaa"
