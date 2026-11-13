"""Behaviour when FastAPI is not importable (pure Starlette app)."""

import importlib
import sys

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from fastapi_perfstats import StatsCollector, StatsConfig, mount_stats, stats_router


@pytest.fixture
def no_fastapi(monkeypatch):
    """Make `import fastapi*` fail and forget already-imported fastapi_perfstats.route."""
    for name in [m for m in sys.modules if m == "fastapi" or m.startswith("fastapi.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.delitem(sys.modules, "fastapi_perfstats.route", raising=False)
    monkeypatch.setitem(sys.modules, "fastapi", None)  # None entry => ImportError on import
    importlib.invalidate_caches()


def starlette_app(collector):
    async def item(request):
        return JSONResponse({"id": request.path_params["item_id"]})

    app = Starlette(routes=[Route("/items/{item_id}", item)])
    app.add_middleware(collector.middleware)
    mount_stats(app, collector)
    return app


def test_middleware_page_and_json_work_on_pure_starlette(no_fastapi):
    c = StatsCollector(StatsConfig(basic_auth=("admin", "change-me")))
    cl = TestClient(starlette_app(c))
    assert cl.get("/items/1").status_code == 200
    assert cl.get("/items/2").status_code == 200
    assert cl.get("/stats").status_code == 401
    assert cl.get("/stats.json", auth=("admin", "wrong")).status_code == 401
    j = cl.get("/stats.json", auth=("admin", "change-me")).json()
    assert j["routes"][0]["key"] == "GET /items/{item_id}" and j["routes"][0]["count"] == 2
    assert "Call frequency" in cl.get("/stats", auth=("admin", "change-me")).text
    assert "fastapi" not in {m.split(".")[0] for m in sys.modules if sys.modules[m] is not None}


def test_stats_route_needs_fastapi_message(no_fastapi):
    import fastapi_perfstats

    with pytest.raises(ImportError, match="StatsRoute needs FastAPI installed"):
        fastapi_perfstats.StatsRoute  # noqa: B018
    with pytest.raises(ImportError, match="StatsRoute needs FastAPI installed"):
        StatsCollector().route_class  # noqa: B018


def test_dependencies_need_fastapi_message(no_fastapi):
    with pytest.raises(ImportError, match="needs FastAPI installed"):
        stats_router(StatsCollector(), dependencies=[object()])
