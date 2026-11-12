import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

import fastapi_stats
from fastapi_stats import StatsCollector, StatsRoute, get_default_collector, set_default_collector


@pytest.fixture(autouse=True)
def reset_default():
    set_default_collector(None)
    yield
    set_default_collector(None)


def by_key(collector):
    return {r.key: r for r in collector.store.routes()}


def build_app():
    app = FastAPI()
    router = APIRouter(route_class=StatsRoute)

    @router.get("/s/{x}")
    async def s(x: int):
        if x == 0:
            raise HTTPException(500, "no")
        return x

    @router.get("/health")
    async def health():
        return 1

    app.include_router(router)
    return TestClient(app)


def test_default_collector_records_templated_key_errors_and_exclusions():
    cl = build_app()
    cl.get("/s/1")
    cl.get("/s/0")
    cl.get("/health")
    k = by_key(get_default_collector())
    assert k["GET /s/{x}"].count == 2 and k["GET /s/{x}"].errors_5xx == 1
    assert "GET /health" not in k


def test_default_collector_swapped_at_runtime():
    cl = build_app()
    first = get_default_collector()
    cl.get("/s/1")
    mine = StatsCollector()
    set_default_collector(mine)
    cl.get("/s/2")
    assert by_key(mine)["GET /s/{x}"].count == 1 and by_key(first)["GET /s/{x}"].count == 1


def test_collector_route_class_is_bound_and_isolated():
    c = StatsCollector()
    assert issubclass(c.route_class, StatsRoute) and c.route_class.collector is c
    assert StatsRoute.collector is None
    app = FastAPI()
    router = APIRouter(route_class=c.route_class)

    @router.get("/o")
    async def o():
        return 1

    app.include_router(router)
    TestClient(app).get("/o")
    assert by_key(c)["GET /o"].count == 1 and by_key(get_default_collector()) == {}


def test_unknown_attribute_raises():
    with pytest.raises(AttributeError):
        fastapi_stats.Nope  # noqa: B018
    assert fastapi_stats.make_route_class(StatsCollector()).collector is not None
