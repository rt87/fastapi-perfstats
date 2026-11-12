import base64

import pytest
from prometheus_client.parser import text_string_to_metric_families
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from fakes import RecordingStore, make_call
from fastapi_stats import StatsCollector, StatsConfig, mount_stats
from fastapi_stats.prometheus import render_prometheus


def parse(text):
    return {family.name: family for family in text_string_to_metric_families(text)}


def samples(text, name):
    return [s for family in parse(text).values() for s in family.samples if s.name == name]


class DroppingStore(RecordingStore):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def routes(self):
        return self.rows

    @property
    def dropped(self):
        return 3


def collector(**config):
    c = StatsCollector(StatsConfig(prometheus=True, **config))
    for ms, status in ((100, 200), (200, 200), (300, 404), (400, 500)):
        c.store.record(make_call("GET /items/{id}", ms, status))
    c.store.record(make_call("POST /items", 50, 201))
    return c


def test_families_and_values_parse():
    families = parse(render_prometheus(collector()))
    assert set(families) == {
        "fastapi_stats_requests",
        "fastapi_stats_responses_errors",
        "fastapi_stats_request_duration_seconds",
    }
    assert families["fastapi_stats_requests"].type == "counter"
    assert families["fastapi_stats_request_duration_seconds"].type == "summary"
    text = render_prometheus(collector())
    by_route = {s.labels["route"]: s for s in samples(text, "fastapi_stats_requests_total")}
    assert by_route["/items/{id}"].value == 4 and by_route["/items/{id}"].labels["method"] == "GET"
    assert by_route["/items"].value == 1 and by_route["/items"].labels["method"] == "POST"
    errors = {
        (s.labels["route"], s.labels["status_class"]): s.value
        for s in samples(text, "fastapi_stats_responses_errors_total")
    }
    assert errors[("/items/{id}", "4xx")] == 1 and errors[("/items/{id}", "5xx")] == 1
    assert errors[("/items", "4xx")] == 0


def test_durations_are_seconds_and_quantiles_follow_config():
    c = collector(quantiles=(0.5, 0.99))
    text = render_prometheus(c)
    total = next(
        s for s in samples(text, "fastapi_stats_request_duration_seconds_sum") if s.labels["route"] != "/items"
    )
    assert total.value == pytest.approx(1.0)  # 100+200+300+400 ms
    count = next(
        s for s in samples(text, "fastapi_stats_request_duration_seconds_count") if s.labels["route"] != "/items"
    )
    assert count.value == 4
    quantiles = {
        s.labels["quantile"]: s.value
        for s in samples(text, "fastapi_stats_request_duration_seconds")
        if s.labels["route"] == "/items/{id}"
    }
    assert set(quantiles) == {"0.5", "0.99"}
    assert all(0 < v < 1 for v in quantiles.values())


def test_every_family_has_help_and_type():
    lines = render_prometheus(collector()).splitlines()
    for name in (
        "fastapi_stats_requests_total",
        "fastapi_stats_responses_errors_total",
        "fastapi_stats_request_duration_seconds",
    ):
        assert any(line.startswith(f"# HELP {name} ") for line in lines)
        assert any(line == f"# TYPE {name} {'summary' if name.endswith('seconds') else 'counter'}" for line in lines)


def test_custom_prefix():
    text = render_prometheus(collector(prometheus_prefix="svc:stats"))
    assert samples(text, "svc:stats_requests_total")
    assert "fastapi_stats_" not in text


def test_hostile_route_is_escaped_and_roundtrips():
    route = 'a"b\\c\nd'
    c = StatsCollector(StatsConfig(prometheus=True, key_func=lambda scope: f"GET {route}"))
    c.store.record(make_call(f"GET {route}", 10, 200))
    text = render_prometheus(c)
    assert 'route="a\\"b\\\\c\\nd"' in text
    assert "\nd" not in text.replace("\\nd", "")  # no raw newline inside the label
    (sample,) = samples(text, "fastapi_stats_requests_total")
    assert sample.labels["route"] == route


def test_empty_collector_renders_nothing():
    assert render_prometheus(StatsCollector(StatsConfig(prometheus=True))) == ""


def test_dropped_counter_only_when_nonzero():
    c = collector()
    assert "dropped" not in render_prometheus(c)
    c.store = DroppingStore(c.store.routes())
    (sample,) = samples(render_prometheus(c), "fastapi_stats_dropped_records_total")
    assert sample.value == 3


def test_quantile_missing_from_row_is_skipped():
    c = collector(quantiles=(0.5,))
    c.config = StatsConfig(prometheus=True, quantiles=(0.5, 0.9, 0.99))
    text = render_prometheus(c)
    assert {s.labels["quantile"] for s in samples(text, "fastapi_stats_request_duration_seconds")} == {"0.5"}


def app_for(c):
    async def item(request):
        return JSONResponse({})

    app = Starlette(routes=[Route("/items/{item_id}", item)])
    app.add_middleware(c.middleware)
    mount_stats(app, c)
    return TestClient(app)


def test_endpoint_serves_text_format_and_is_not_recorded():
    c = StatsCollector(StatsConfig(prometheus=True))
    client = app_for(c)
    client.get("/items/1")
    response = client.get("/stats/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert [s.value for s in samples(response.text, "fastapi_stats_requests_total")] == [1]
    client.get("/stats/metrics")
    assert [r.key for r in c.store.routes()] == ["GET /items/{item_id}"]


def test_endpoint_disabled_by_default():
    assert app_for(StatsCollector(StatsConfig())).get("/stats/metrics").status_code == 404


def test_endpoint_requires_auth_when_configured():
    c = StatsCollector(StatsConfig(prometheus=True, basic_auth=("admin", "s3cret")))
    client = app_for(c)
    assert client.get("/stats/metrics").status_code == 401
    good = {"Authorization": "Basic " + base64.b64encode(b"admin:s3cret").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()}
    assert client.get("/stats/metrics", headers=bad).status_code == 401
    assert client.get("/stats/metrics", headers=good).status_code == 200


@pytest.mark.parametrize("prefix", ["", "1abc", "a-b", "a b", "ä"])
def test_invalid_prefix_raises(prefix):
    with pytest.raises(ValueError, match="prometheus_prefix"):
        StatsConfig(prometheus_prefix=prefix)
