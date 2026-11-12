"""Mount prefix in route keys (#4)."""

from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from fastapi_stats import StatsCollector, StatsConfig
from fastapi_stats.mounts import mount_prefix


async def item(request: Request) -> JSONResponse:
    return JSONResponse({"id": request.path_params["item_id"]})


async def plain(scope: Any, receive: Any, send: Any) -> None:
    await PlainTextResponse("x")(scope, receive, send)


def inner_routes() -> list[Route]:
    return [Route("/items/{item_id}", item)]


def keys_of(collector: StatsCollector) -> set[str]:
    return {r.key for r in collector.store.routes()}


def starlette_app(collector: StatsCollector, routes: list[Any]) -> Starlette:
    app = Starlette(routes=routes)
    app.add_middleware(collector.middleware)
    return app


@pytest.fixture
def collector() -> StatsCollector:
    return StatsCollector(StatsConfig())


def test_two_mounts_with_same_inner_route_get_separate_keys(collector: StatsCollector) -> None:
    app = starlette_app(collector, [Mount("/v1", routes=inner_routes()), Mount("/v2", routes=inner_routes())])
    cl = TestClient(app)
    cl.get("/v1/items/1")
    cl.get("/v2/items/2")
    assert keys_of(collector) == {"GET /v1/items/{item_id}", "GET /v2/items/{item_id}"}


def test_nested_mounts_produce_full_prefix(collector: StatsCollector) -> None:
    app = starlette_app(collector, [Mount("/api", routes=[Mount("/v1", routes=inner_routes())])])
    TestClient(app).get("/api/v1/items/3")
    assert keys_of(collector) == {"GET /api/v1/items/{item_id}"}


def test_fastapi_include_router_prefix_is_not_doubled(collector: StatsCollector) -> None:
    app = FastAPI()
    router = APIRouter(prefix="/api")

    @router.get("/items/{item_id}")
    def get_item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    app.include_router(router)
    app.add_middleware(collector.middleware)
    TestClient(app).get("/api/items/4")
    assert keys_of(collector) == {"GET /api/items/{item_id}"}


def test_fastapi_with_mounted_subapp(collector: StatsCollector) -> None:
    sub = FastAPI()

    @sub.get("/items/{item_id}")
    def get_item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    app = FastAPI()
    app.mount("/v1", sub)
    app.add_middleware(collector.middleware)
    TestClient(app).get("/v1/items/4")
    assert keys_of(collector) == {"GET /v1/items/{item_id}"}


def test_mount_without_inner_routing_gives_mount_path(collector: StatsCollector) -> None:
    app = starlette_app(collector, [Mount("/static", app=plain)])
    TestClient(app).get("/static/a/b.css")
    assert keys_of(collector) == {"GET /static"}


def test_nested_mount_without_inner_routing(collector: StatsCollector) -> None:
    app = starlette_app(collector, [Mount("/a", routes=[Mount("/b", app=plain)])])
    TestClient(app).get("/a/b/file")
    assert keys_of(collector) == {"GET /a/b"}


def test_proxy_root_path_never_appears_in_keys(collector: StatsCollector) -> None:
    app = starlette_app(collector, [Mount("/v1", routes=inner_routes())])
    TestClient(app, root_path="/proxy").get("/proxy/v1/items/1")
    assert keys_of(collector) == {"GET /v1/items/{item_id}"}


def test_plain_top_level_route_has_no_prefix(collector: StatsCollector) -> None:
    app = starlette_app(collector, [Route("/items/{item_id}", item)])
    TestClient(app, root_path="/proxy").get("/proxy/items/1")
    assert keys_of(collector) == {"GET /items/{item_id}"}


def test_unknown_path_below_mount_is_not_an_item_key(collector: StatsCollector) -> None:
    app = starlette_app(collector, [Mount("/v1", routes=inner_routes())])
    assert TestClient(app).get("/v1/nothing/here").status_code == 404
    assert keys_of(collector) == {"GET /v1"}


def test_unknown_top_level_path_is_keyed_by_raw_path(collector: StatsCollector) -> None:
    app = starlette_app(collector, [Mount("/v1", routes=inner_routes())])
    TestClient(app).get("/zzz")
    assert keys_of(collector) == {"GET /zzz"}


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"root_path": "/x"},
        {"app_root_path": "/other", "root_path": "/x"},
    ],
)
def test_mount_prefix_is_empty_without_usable_mount_info(scope: dict[str, str]) -> None:
    assert mount_prefix(scope) == ""


def test_mount_prefix_strips_app_root_path() -> None:
    assert mount_prefix({"app_root_path": "/proxy", "root_path": "/proxy/v1"}) == "/v1"
    assert mount_prefix({"app_root_path": "", "root_path": "/v1/v2"}) == "/v1/v2"


def test_mount_path_parameters_leak_raw_value_documented_limitation(collector: StatsCollector) -> None:
    app = starlette_app(collector, [Mount("/tenants/{tenant}", routes=inner_routes())])
    TestClient(app).get("/tenants/acme/items/1")
    assert keys_of(collector) == {"GET /tenants/acme/items/{item_id}"}
