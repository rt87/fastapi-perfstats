"""Reset (#10): store ordering, the opt-in endpoint with its CSRF guard, and the page button."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from fakes import RecordingStore, make_call
from fastapi_stats import reset
from fastapi_stats.collector import StatsCollector
from fastapi_stats.config import StatsConfig
from fastapi_stats.page import mount_stats, render_html, script_text

GUARD = {"X-Stats-Reset": "1"}


def _config(kind: str, tmp_path: Path, **fields: object) -> StatsConfig:
    if kind == "sqlite":
        return StatsConfig(storage="sqlite", sqlite_path=str(tmp_path / "r.db"), **fields)  # type: ignore[arg-type]
    return StatsConfig(**fields)  # type: ignore[arg-type]


@pytest.fixture(params=["memory", "sqlite"])
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _client(collector: StatsCollector) -> TestClient:
    app = Starlette()
    app.add_middleware(collector.middleware)
    mount_stats(app, collector)
    return TestClient(app)


# ---- store behaviour through the collector -----------------------------------------------------------------
def test_data_before_reset_is_gone_and_data_after_is_kept(kind: str, tmp_path: Path) -> None:
    collector = StatsCollector(_config(kind, tmp_path))
    try:
        collector.record_call(make_call(key="GET /old"))
        collector.record_call(make_call(key="GET /old", duration_ms=800.0))
        collector.reset()
        collector.record_call(make_call(key="GET /new"))
        assert [r.key for r in collector.store.routes()] == ["GET /new"]
        assert collector.store.slow_calls() == []
    finally:
        collector.close()


# ---- endpoint ----------------------------------------------------------------------------------------------
def test_endpoint_is_absent_by_default(kind: str, tmp_path: Path) -> None:
    collector = StatsCollector(_config(kind, tmp_path))
    try:
        response = _client(collector).post("/stats/reset", headers=GUARD)
        assert response.status_code in (404, 405)
    finally:
        collector.close()


def test_reset_succeeds_with_204_and_no_store(kind: str, tmp_path: Path) -> None:
    collector = StatsCollector(_config(kind, tmp_path, allow_reset=True))
    try:
        collector.record_call(make_call(key="GET /old"))
        response = _client(collector).post("/stats/reset", headers=GUARD)
        assert response.status_code == 204
        assert response.headers["cache-control"] == "no-store"
        assert collector.store.routes() == []
    finally:
        collector.close()


def test_missing_or_wrong_header_is_400_and_keeps_data() -> None:
    collector = StatsCollector(StatsConfig(allow_reset=True))
    collector.record_call(make_call())
    client = _client(collector)
    assert client.post("/stats/reset").status_code == 400
    assert client.post("/stats/reset", headers={"X-Stats-Reset": "0"}).status_code == 400
    assert len(collector.store.routes()) == 1


def test_get_is_405() -> None:
    collector = StatsCollector(StatsConfig(allow_reset=True))
    assert _client(collector).get("/stats/reset").status_code == 405


def test_bad_credentials_are_401_and_keep_data() -> None:
    collector = StatsCollector(StatsConfig(allow_reset=True, basic_auth=("admin", "pw")))
    collector.record_call(make_call())
    client = _client(collector)
    assert client.post("/stats/reset", headers=GUARD).status_code == 401
    assert client.post("/stats/reset", headers=GUARD, auth=("admin", "wrong")).status_code == 401
    assert len(collector.store.routes()) == 1
    assert client.post("/stats/reset", headers=GUARD, auth=("admin", "pw")).status_code == 204
    assert collector.store.routes() == []


def test_forbidden_by_access_check_is_403() -> None:
    collector = StatsCollector(StatsConfig(allow_reset=True, access=lambda request: False))
    collector.record_call(make_call())
    assert _client(collector).post("/stats/reset", headers=GUARD).status_code == 403
    assert len(collector.store.routes()) == 1


def test_store_without_reset_support_is_501() -> None:
    collector = StatsCollector(StatsConfig(allow_reset=True, storage=RecordingStore()))
    assert _client(collector).post("/stats/reset", headers=GUARD).status_code == 501


def test_reset_request_is_not_recorded() -> None:
    collector = StatsCollector(StatsConfig(allow_reset=True))
    client = _client(collector)
    assert client.post("/stats/reset", headers=GUARD).status_code == 204
    assert client.post("/stats/reset").status_code == 400
    assert collector.store.routes() == []


# ---- page --------------------------------------------------------------------------------------------------
def test_button_only_when_enabled() -> None:
    off = StatsCollector(StatsConfig())
    on = StatsCollector(StatsConfig(allow_reset=True))
    assert "Reset statistics" not in render_html(off)
    assert 'data-reset-url="' not in render_html(off)
    html = render_html(on)
    assert '<button type="button" id="stats-reset">Reset statistics</button>' in html
    assert 'data-reset-url="/stats/reset"' in html


def test_reset_url_follows_stats_path() -> None:
    html = render_html(StatsCollector(StatsConfig(allow_reset=True, stats_path="/admin/stats/")))
    assert 'data-reset-url="/admin/stats/reset"' in html


def test_script_confirms_and_sends_header() -> None:
    script = script_text(StatsCollector(StatsConfig(allow_reset=True)))
    assert "confirm(" in script
    assert "'X-Stats-Reset':'1'" in script
    assert "location.reload()" in script
    assert reset.PART.js in script


def test_page_html_is_served_with_button() -> None:
    collector = StatsCollector(StatsConfig(allow_reset=True))
    body = _client(collector).get("/stats").text
    assert 'id="stats-reset"' in body
