"""Slow-call context: redacted query, request / trace id, custom hook, columns and persistence."""

import logging
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from fakes import make_call, make_slow_call
from fastapi_stats import StatsCollector, StatsConfig, render_html, render_json
from fastapi_stats import slow_context as sc
from fastapi_stats.models import SlowExtra
from fastapi_stats.stores import MemoryStore, SqliteStore, StatsStore

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


@pytest.fixture(autouse=True)
def fresh_error_log() -> None:
    sc._logged_hook_errors.clear()


def scope(query: str = "", headers: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "GET",
        "path": "/search",
        "query_string": query.encode(),
        "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
    }


def cap(config: StatsConfig, s: dict[str, Any]) -> SlowExtra:
    return sc.capture(s, config, "GET /search", 900.0)


# -- query -----------------------------------------------------------------------------------------------------


def test_default_config_stores_no_query_but_request_id() -> None:
    extra = cap(StatsConfig(), scope("q=shoes", {"X-Request-Id": "req-1"}))
    assert extra.query is None
    assert extra.request_id == "req-1"
    assert extra.context is None


def test_query_is_redacted_when_capture_is_on() -> None:
    extra = cap(StatsConfig(slow_capture_query=True), scope("q=shoes&apiKey=abc123&sessionId=zz9"))
    assert extra.query == "q=shoes&apiKey=%5Bredacted%5D&sessionId=%5Bredacted%5D"


def test_blank_values_kept_and_empty_query_is_none() -> None:
    config = StatsConfig(slow_capture_query=True)
    assert cap(config, scope("a=&b=1")).query == "a=&b=1"
    assert cap(config, scope("")).query is None


def test_custom_redaction_list_is_case_insensitive() -> None:
    config = StatsConfig(slow_capture_query=True, redact_query_params=("EMAIL",))
    assert cap(config, scope("Email=x%40y.z&token=keep")).query == "Email=%5Bredacted%5D&token=keep"


def test_query_truncated_to_500_chars() -> None:
    extra = cap(StatsConfig(slow_capture_query=True), scope("q=" + "x" * 900))
    assert extra.query is not None and len(extra.query) == sc.MAX_QUERY_LENGTH


def test_undecodable_query_does_not_raise() -> None:
    s = scope()
    s["query_string"] = b"q=\xff\xfe"
    assert cap(StatsConfig(slow_capture_query=True), s).query is not None


def test_missing_query_string_and_headers_in_scope() -> None:
    extra = sc.capture({"type": "http"}, StatsConfig(slow_capture_query=True), "k", 1.0)
    assert extra == SlowExtra()


# -- request id ------------------------------------------------------------------------------------------------


def test_traceparent_keeps_only_the_trace_id() -> None:
    assert cap(StatsConfig(), scope(headers={"traceparent": TRACEPARENT})).request_id == TRACE_ID


@pytest.mark.parametrize("bad", ["garbage", "00", f"00-{TRACE_ID[:-1]}-x-01", "00-" + "z" * 32 + "-x-01"])
def test_invalid_traceparent_is_ignored(bad: str) -> None:
    assert cap(StatsConfig(), scope(headers={"traceparent": bad})).request_id is None


def test_first_present_header_wins_and_invalid_traceparent_falls_through() -> None:
    config = StatsConfig(slow_request_id_headers=("traceparent", "X-Correlation-Id"))
    assert cap(config, scope(headers={"traceparent": "bad", "x-correlation-id": "c-9"})).request_id == "c-9"
    config = StatsConfig()
    both = {"x-request-id": "first", "traceparent": TRACEPARENT}
    assert cap(config, scope(headers=both)).request_id == "first"


def test_request_id_is_truncated_and_blank_ignored() -> None:
    assert cap(StatsConfig(), scope(headers={"x-request-id": "a" * 500})).request_id == "a" * 128
    assert cap(StatsConfig(), scope(headers={"x-request-id": "   "})).request_id is None


def test_no_id_headers_configured() -> None:
    config = StatsConfig(slow_request_id_headers=())
    assert cap(config, scope(headers={"x-request-id": "r"})).request_id is None


# -- context hook ----------------------------------------------------------------------------------------------


def test_hook_result_is_stored() -> None:
    config = StatsConfig(slow_context=lambda s: {"tenant": "acme", "n": 5})  # type: ignore[dict-item]
    assert cap(config, scope()).context == {"tenant": "acme", "n": "5"}


def test_hook_result_is_truncated() -> None:
    big = {f"k{i}": "v" * 300 for i in range(25)}
    context = cap(StatsConfig(slow_context=lambda s: big), scope()).context
    assert context is not None and len(context) == sc.MAX_CONTEXT_ITEMS
    assert all(len(v) == sc.MAX_CONTEXT_TEXT for v in context.values())
    long_key = cap(StatsConfig(slow_context=lambda s: {"k" * 300: "v"}), scope()).context
    assert long_key == {"k" * 200: "v"}


def test_raising_hook_records_context_error_and_logs_once_per_type(caplog: pytest.LogCaptureFixture) -> None:
    def boom(s: Mapping[str, Any]) -> Mapping[str, str]:
        raise KeyError("x")

    caplog.set_level(logging.WARNING, logger="fastapi_stats")
    config = StatsConfig(slow_context=boom)
    assert cap(config, scope()).context == {"context_error": "KeyError"}
    assert cap(config, scope()).context == {"context_error": "KeyError"}
    assert caplog.text.count("slow_context hook raised KeyError") == 1

    def other(s: Mapping[str, Any]) -> Mapping[str, str]:
        raise ValueError("y")

    cap(StatsConfig(slow_context=other), scope())
    assert "slow_context hook raised ValueError" in caplog.text


def test_hook_returning_garbage_is_contained() -> None:
    config = StatsConfig(slow_context=lambda s: None)  # type: ignore[arg-type,return-value]
    assert cap(config, scope()).context == {"context_error": "AttributeError"}


# -- end to end through the collector ------------------------------------------------------------------------------


def test_hook_never_called_for_fast_calls() -> None:
    calls: list[int] = []

    def hook(s: Mapping[str, Any]) -> Mapping[str, str]:
        calls.append(1)
        return {"a": "b"}

    collector = StatsCollector(StatsConfig(slow_threshold_ms=100, slow_context=hook, slow_capture_query=True))
    collector.finish(scope("q=1"), 5.0, 200)
    assert calls == []
    collector.finish(scope("q=1"), 150.0, 200)
    assert calls == [1]
    (slow,) = collector.store.slow_calls()
    assert slow.context == {"a": "b"} and slow.query == "q=1"


async def search(request: Request) -> Response:
    return PlainTextResponse("ok")


def test_end_to_end_with_middleware_and_raising_hook() -> None:
    def boom(s: Mapping[str, Any]) -> Mapping[str, str]:
        raise RuntimeError("nope")

    collector = StatsCollector(
        StatsConfig(slow_threshold_ms=0, slow_capture_query=True, slow_context=boom, exclude_paths=())
    )
    app = Starlette(routes=[Route("/search", search)])
    app.add_middleware(collector.middleware)
    response = TestClient(app).get("/search?q=a&password=hunter2", headers={"x-request-id": "abc"})
    assert response.status_code == 200  # a failing hook never breaks the request
    (slow,) = collector.store.slow_calls()
    assert slow.request_id == "abc"
    assert slow.query == "q=a&password=%5Bredacted%5D"
    assert slow.context == {"context_error": "RuntimeError"}


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StatsStore]:
    config = StatsConfig(slow_threshold_ms=0, sqlite_path=str(tmp_path / "s.db"))
    s: StatsStore = MemoryStore(config) if request.param == "memory" else SqliteStore(config)
    yield s
    s.close()


def test_both_stores_return_the_new_fields(store: StatsStore) -> None:
    store.record(make_call(query="a=1", request_id="r", context={"k": "v"}))
    store.record(make_call())
    plain, full = reversed(store.slow_calls())
    assert (plain.query, plain.request_id, plain.context) == ("a=1", "r", {"k": "v"})
    assert (full.query, full.request_id, full.context) == (None, None, {})


# -- page --------------------------------------------------------------------------------------------------------


def slow_table(html: str) -> str:
    return html.split('id="slow"')[1].split("</table>")[0]


def test_columns_render_and_escape_a_malicious_query() -> None:
    collector = StatsCollector(StatsConfig(slow_threshold_ms=0))
    collector.store.record(
        make_call(
            query='"><script>alert(1)</script>',
            request_id="<b>id</b>",
            context={"k": "<i>v</i>", "k2": "v2"},
        )
    )
    table = slow_table(render_html(collector))
    for header in ("Request id", "Query", "Context"):
        assert f"<th>{header}</th>" in table
    assert "<script>alert(1)" not in table and "&lt;script&gt;alert(1)" in table
    assert "&lt;b&gt;id&lt;/b&gt;" in table
    assert "k=&lt;i&gt;v&lt;/i&gt;; k2=v2" in table


def test_columns_render_empty_for_plain_slow_call() -> None:
    config = StatsConfig()
    cells = [c.cell(make_slow_call(), config) for c in sc.EXT.columns(config)]
    assert cells == [("", None), ("", None), ("", None)]


def test_json_has_the_fields() -> None:
    collector = StatsCollector(StatsConfig(slow_threshold_ms=0))
    collector.store.record(make_call(query="a=1", request_id="r", context={"k": "v"}))
    (item,) = render_json(collector)["slow_calls"]
    assert (item["query"], item["request_id"], item["context"]) == ("a=1", "r", {"k": "v"})
