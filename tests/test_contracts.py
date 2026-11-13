"""Stage-0 contracts: config fields and validation, shared types and registries."""

import asyncio
import dataclasses
import sqlite3
from dataclasses import FrozenInstanceError, asdict
from types import MappingProxyType

import pytest
from starlette.requests import Request

import fastapi_perfstats
from fakes import RecordingStore, make_call, make_route_stats, make_slow_call
from fastapi_perfstats import (
    StatsCollector,
    StatsConfig,
    auth,
    keys,
    lifecycle,
    live,
    mounts,
    parts,
    prometheus,
    render_json,
    reset,
    slow_context,
)
from fastapi_perfstats.ext import EXTENSIONS, recent, sizes, stddev
from fastapi_perfstats.ext.base import Column, EndpointSpec, Extension, NoopExtension, PagePart
from fastapi_perfstats.models import (
    CLIENT_CLOSED_STATUS,
    KNOWN_METHODS,
    OTHER_METHOD,
    UNMATCHED_ROUTE,
    CallRecord,
    RecentStats,
    SlowExtra,
)
from fastapi_perfstats.sqlite_schema import SCHEMA_VERSION, ensure_schema
from fastapi_perfstats.stores import MemoryStore, StatsStore

# -- config ------------------------------------------------------------------------------------------------


def test_config_new_field_defaults():
    c = StatsConfig()
    assert c.unmatched == "group"
    assert c.close_on_shutdown is True
    assert c.access is None
    assert c.allow_reset is False
    assert dict(c.slow_thresholds) == {}
    assert c.slow_capture_query is False
    assert c.redact_query_params == ("pass", "tok", "secret", "key", "auth", "sig", "session", "code")
    assert c.slow_request_id_headers == ("x-request-id", "traceparent")
    assert c.slow_context is None
    assert c.track_sizes is True
    assert c.recent_minutes == 60
    assert c.recent_windows == (1, 5, 15)
    assert c.refresh_seconds is None
    assert c.prometheus is False
    assert c.prometheus_prefix == "fastapi_perfstats"


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"unmatched": "nope"}, "unmatched"),
        ({"slow_thresholds": {"": 1.0}}, "slow_thresholds"),
        ({"slow_thresholds": {"GET /a": -1.0}}, "slow_thresholds"),
        ({"recent_minutes": -1}, "recent_minutes"),
        ({"recent_minutes": 1441}, "recent_minutes"),
        ({"recent_windows": (0,)}, "recent_windows"),
        ({"recent_minutes": 10, "recent_windows": (11,)}, "recent_windows"),
        ({"refresh_seconds": 7}, "refresh_seconds"),
        ({"prometheus_prefix": "1bad"}, "prometheus_prefix"),
        ({"prometheus_prefix": ""}, "prometheus_prefix"),
        ({"prometheus_prefix": "has-dash"}, "prometheus_prefix"),
    ],
)
def test_config_validation_error_names_the_field(kwargs, field):
    with pytest.raises(ValueError, match=field):
        StatsConfig(**kwargs)


def test_config_normalisation():
    c = StatsConfig(
        slow_thresholds={"GET /a": 5.0, "GET /b": None},
        redact_query_params=("PASS", "Tok"),
        slow_request_id_headers=("X-Request-Id",),
        recent_windows=(15, 1, 5, 5),
    )
    assert isinstance(c.slow_thresholds, MappingProxyType)
    assert c.slow_thresholds["GET /b"] is None
    assert c.redact_query_params == ("pass", "tok")
    assert c.slow_request_id_headers == ("x-request-id",)
    assert c.recent_windows == (1, 5, 15)
    with pytest.raises(TypeError):
        c.slow_thresholds["GET /c"] = 1.0  # type: ignore[index]


def test_config_accepts_valid_choices():
    for seconds in (None, 0, 5, 15, 60):
        assert StatsConfig(refresh_seconds=seconds).refresh_seconds == seconds
    for mode in ("group", "raw", "skip"):
        assert StatsConfig(unmatched=mode).unmatched == mode
    assert StatsConfig(prometheus_prefix="a:b_c9").prometheus_prefix == "a:b_c9"


def test_recent_windows_ignored_when_recent_minutes_is_zero():
    assert StatsConfig(recent_minutes=0, recent_windows=(500,)).recent_windows == (500,)


def test_slow_threshold_defaults():
    assert StatsConfig().slow_threshold_for("GET /x") == 500.0
    assert StatsConfig().slow_threshold_summary() == ">= 500 ms"
    off = StatsConfig(slow_threshold_ms=None)
    assert off.slow_threshold_for("GET /x") is None
    assert off.slow_threshold_summary() == "disabled"


# -- models ------------------------------------------------------------------------------------------------


def test_constants():
    assert UNMATCHED_ROUTE == "<unmatched>"
    assert OTHER_METHOD == "OTHER"
    assert CLIENT_CLOSED_STATUS == 499
    assert "GET" in KNOWN_METHODS
    assert len(KNOWN_METHODS) == 9


def test_call_record_is_frozen_with_defaults():
    call = make_call()
    assert call.method == ""
    assert call.path == ""
    assert call.request_bytes is None
    assert call.response_bytes is None
    assert call.query is None
    assert call.request_id is None
    assert call.context is None
    with pytest.raises(FrozenInstanceError):
        call.status = 500  # type: ignore[misc]
    assert not hasattr(call, "__dict__")  # slots


def test_slow_extra_is_frozen():
    extra = SlowExtra()
    assert (extra.query, extra.request_id, extra.context) == (None, None, None)
    with pytest.raises(FrozenInstanceError):
        extra.query = "x"  # type: ignore[misc]


def test_route_stats_new_defaults():
    r = make_route_stats()
    assert r.error_rate_4xx == 0.0
    assert r.error_rate_5xx == 0.0
    assert r.stddev_ms is None
    assert r.mean_request_bytes is None
    assert r.mean_response_bytes is None
    assert r.max_response_bytes is None
    assert r.recent == {}
    assert r.series == []
    assert make_route_stats().recent is not r.recent  # no shared mutable default


def test_slow_call_new_defaults():
    s = make_slow_call()
    assert (s.query, s.request_id, s.context) == (None, None, {})
    assert make_slow_call().context is not s.context


def test_json_field_names():
    store = MemoryStore(StatsConfig(slow_threshold_ms=0))
    collector = StatsCollector(StatsConfig(storage=store, slow_threshold_ms=0))
    collector.record("GET /a", 1.0, 200)
    data = render_json(collector)
    route_fields = {f.name for f in dataclasses.fields(make_route_stats())}
    slow_fields = {f.name for f in dataclasses.fields(make_slow_call())}
    assert set(data["routes"][0]) == route_fields | {"method", "route"}
    assert set(data["slow_calls"][0]) == slow_fields | {"method", "route"}
    recent = RecentStats(calls=1, calls_per_min=1.0, errors_5xx=0, error_rate_5xx=0.0, mean_ms=1.0)
    assert set(asdict(recent)) == {"calls", "calls_per_min", "errors_5xx", "error_rate_5xx", "mean_ms"}


# -- store ABC ---------------------------------------------------------------------------------------------


def test_store_abc_defaults():
    store = RecordingStore()
    assert store.dropped == 0
    with pytest.raises(NotImplementedError, match="RecordingStore"):
        store.reset()
    store.record(make_call())
    assert store.records == [make_call()]
    assert store.routes() == [] and store.slow_calls() == []


def test_collector_record_builds_a_call_record():
    store = RecordingStore()
    StatsCollector(StatsConfig(storage=store)).record("GET /a", 12.5, 404)
    (call,) = store.records
    assert isinstance(call, CallRecord)
    assert (call.key, call.duration_ms, call.status) == ("GET /a", 12.5, 404)
    assert call.ts > 0


def test_abstract_record_takes_a_call_record():
    assert "call" in StatsStore.record.__annotations__


# -- registries and stubs ----------------------------------------------------------------------------------


def test_registries_in_order():
    assert EXTENSIONS == (stddev.EXT, sizes.EXT, recent.EXT, slow_context.EXT, live.EXT)
    assert [e.name for e in EXTENSIONS] == ["stddev", "sizes", "recent", "slow_context", "live"]
    assert parts.PAGE_PARTS == (lifecycle.PART, reset.PART, live.PART)
    assert parts.ENDPOINTS == (reset.ENDPOINT, prometheus.ENDPOINT)
    assert all(isinstance(p, PagePart) for p in parts.PAGE_PARTS)
    assert all(isinstance(e, EndpointSpec) for e in parts.ENDPOINTS)


@pytest.mark.parametrize("ext", [*EXTENSIONS, NoopExtension()], ids=lambda e: e.name)
def test_every_extension_satisfies_the_protocol(ext: Extension):
    config = StatsConfig()
    db = sqlite3.connect(":memory:")
    row = make_route_stats()
    state: dict[str, object] = {}
    for statement in ext.sqlite_schema():
        db.execute(statement)
    ext.memory_update(state, make_call())
    ext.memory_finish(row, state, config, 0.0)
    ext.sqlite_update(db, make_call())
    ext.sqlite_prune(db, config, 0.0)
    ext.sqlite_finish({row.key: row}, db, config, 0.0)
    ext.sqlite_reset(db)
    assert all(isinstance(c, Column) for c in ext.columns(config))
    expected = make_route_stats()
    if ext.name == "stddev":  # real behaviour (#11): one 10 ms call has a stddev of 0.0, rates stay 0.0
        expected.stddev_ms = 0.0
    if ext.name == "recent":  # real behaviour (#15): windows and a zero-filled series are always filled in
        assert set(row.recent) == {"1m", "5m", "15m"} and row.series == [0] * 60
        row.recent, row.series = {}, []
    assert row == expected
    db.close()


def test_page_part_and_endpoint_defaults():
    part = PagePart(name="x", html_top=lambda collector: "")
    assert part.js == ""
    assert part.body_attrs(StatsConfig()) == {}
    assert reset.ENDPOINT.suffix == "/reset" and reset.ENDPOINT.methods == ("POST",)
    assert prometheus.ENDPOINT.suffix == "/metrics" and prometheus.ENDPOINT.methods == ("GET",)
    assert reset.ENDPOINT.enabled(StatsConfig(allow_reset=True)) is True
    assert reset.ENDPOINT.enabled(StatsConfig()) is False
    assert prometheus.ENDPOINT.enabled(StatsConfig(prometheus=True)) is True
    assert prometheus.ENDPOINT.enabled(StatsConfig()) is False


def test_reset_endpoint_requires_header_and_prometheus_serves_text():
    async def run():
        collector = StatsCollector(StatsConfig())
        request = Request({"type": "http", "method": "POST", "path": "/stats/reset", "headers": []})
        response = await reset.ENDPOINT.handler(collector)(request)
        assert response.status_code == 400
        response = await prometheus.ENDPOINT.handler(collector)(request)
        assert response.status_code == 200
        assert response.media_type == prometheus.CONTENT_TYPE
        assert response.headers["content-type"] == prometheus.CONTENT_TYPE
        assert bytes(response.body).startswith(b"# HELP")

    asyncio.run(run())


def test_default_config_key_and_hook_basics():
    config = StatsConfig()
    scope = {"type": "http", "method": "GET", "path": "/raw"}
    assert keys.build_key(scope, StatsConfig(unmatched="raw"), "") == "GET /raw"
    assert keys.build_key({**scope, "route": type("R", (), {"path": "/t/{id}"})()}, config, "/api") == "GET /api/t/{id}"
    assert keys.build_key(scope, StatsConfig(key_func=lambda s: None), "") is None
    assert keys.build_key(scope, StatsConfig(key_func=lambda s: "x" * 500, max_key_length=5), "") == "xxxxx"
    assert mounts.mount_prefix(scope) == ""
    assert slow_context.capture(scope, config, "GET /raw", 1.0) == SlowExtra()
    assert lifecycle.PART.html_top(StatsCollector(config)) == ""
    assert reset.PART.html_top(StatsCollector(config)) == ""


def test_lifespan_without_shutdown_message_reaches_the_app():
    async def run():
        seen = []

        async def app(scope, receive, send):
            seen.append(scope["type"])

        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            return None

        await lifecycle.handle_lifespan(StatsCollector(StatsConfig()), app, {"type": "lifespan"}, receive, send)
        assert seen == ["lifespan"]

    asyncio.run(run())


def test_authorize_matches_basic_auth():
    async def run():
        def request(header: str | None) -> Request:
            raw = [(b"authorization", header.encode())] if header else []
            return Request({"type": "http", "method": "GET", "path": "/stats", "headers": raw})

        assert await auth.authorize(request(None), StatsConfig()) is None
        config = StatsConfig(basic_auth=("u", "p"))
        denied = await auth.authorize(request(None), config)
        assert denied is not None and denied.status_code == 401
        assert denied.headers["www-authenticate"].startswith("Basic")
        assert await auth.authorize(request("Basic dTpw"), config) is None  # u:p
        wrong = await auth.authorize(request("Basic dTp3"), config)  # u:w
        assert wrong is not None and wrong.status_code == 401
        garbage = await auth.authorize(request("Basic !!!"), config)
        assert garbage is not None and garbage.status_code == 401

    asyncio.run(run())


def test_ensure_schema_creates_tables(tmp_path):
    path = tmp_path / "s.db"
    ensure_schema(path)
    ensure_schema(str(path))  # idempotent
    db = sqlite3.connect(path)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    db.close()
    assert {"routes", "durations", "slow_calls"} <= tables
    assert SCHEMA_VERSION == 1


# -- exports -----------------------------------------------------------------------------------------------


def test_exports():
    for name in (
        "CallRecord",
        "SlowExtra",
        "RecentStats",
        "UNMATCHED_ROUTE",
        "OTHER_METHOD",
        "CLIENT_CLOSED_STATUS",
        "AccessCheck",
        "localhost_only",
        "render_prometheus",
        "__version__",
    ):
        assert hasattr(fastapi_perfstats, name), name
        assert name in fastapi_perfstats.__all__
    assert isinstance(fastapi_perfstats.__version__, str) and fastapi_perfstats.__version__
    assert fastapi_perfstats.StatsMiddleware.__module__ == "fastapi_perfstats.middleware"


def test_unknown_attribute_still_raises():
    with pytest.raises(AttributeError):
        fastapi_perfstats.does_not_exist  # noqa: B018
