"""Per-route slow thresholds: lookup order, disabling, validation, both stores, heading and JSON."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from fakes import make_call
from fastapi_perfstats import StatsCollector, StatsConfig, render_html, render_json
from fastapi_perfstats.stores import MemoryStore, SqliteStore, StatsStore

REPORT = "GET /reports/{report_id}"
LOOKUP = "GET /lookup/{item_id}"


def test_exact_key_beats_route_beats_global() -> None:
    config = StatsConfig(
        slow_threshold_ms=500,
        slow_thresholds={REPORT: 3000.0, "/reports/{report_id}": 1000.0, "/lookup/{item_id}": 20.0},
    )
    assert config.slow_threshold_for(REPORT) == 3000.0
    assert config.slow_threshold_for("POST /reports/{report_id}") == 1000.0  # route-only applies to any method
    assert config.slow_threshold_for(LOOKUP) == 20.0
    assert config.slow_threshold_for("GET /other") == 500


def test_none_override_disables_one_route() -> None:
    config = StatsConfig(slow_threshold_ms=500, slow_thresholds={"/reports/{report_id}": None})
    assert config.slow_threshold_for(REPORT) is None
    assert config.slow_threshold_for("GET /other") == 500


def test_global_none_with_route_value_still_applies() -> None:
    config = StatsConfig(slow_threshold_ms=None, slow_thresholds={REPORT: 100.0})
    assert config.slow_threshold_for(REPORT) == 100.0
    assert config.slow_threshold_for("GET /other") is None


def test_keys_without_method_use_whole_key_for_lookup() -> None:
    config = StatsConfig(slow_thresholds={"job": 5.0})
    assert config.slow_threshold_for("job") == 5.0
    assert config.slow_threshold_for("other") == 500.0


@pytest.mark.parametrize("bad", [-1.0, -0.001])
def test_negative_values_raise(bad: float) -> None:
    with pytest.raises(ValueError, match="slow_thresholds"):
        StatsConfig(slow_thresholds={"/a": bad})


def test_empty_key_raises() -> None:
    with pytest.raises(ValueError, match="slow_thresholds"):
        StatsConfig(slow_thresholds={"": 1.0})


def test_mapping_is_copied_and_read_only() -> None:
    source = {"/a": 1.0}
    config = StatsConfig(slow_thresholds=source)
    source["/b"] = 2.0
    assert dict(config.slow_thresholds) == {"/a": 1.0}
    with pytest.raises(TypeError):
        config.slow_thresholds["/c"] = 3.0  # type: ignore[index]


def test_zero_threshold_is_allowed() -> None:
    assert StatsConfig(slow_thresholds={"/a": 0}).slow_threshold_for("GET /a") == 0


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (StatsConfig(), ">= 500 ms"),
        (StatsConfig(slow_threshold_ms=None), "disabled"),
        (StatsConfig(slow_thresholds={"/a": 1.0}), ">= 500 ms, 1 route override"),
        (StatsConfig(slow_thresholds={"/a": 1.0, "/b": None}), ">= 500 ms, 2 route overrides"),
        (StatsConfig(slow_threshold_ms=None, slow_thresholds={"/a": 1.0}), "disabled, 1 route override"),
    ],
)
def test_summary(config: StatsConfig, expected: str) -> None:
    assert config.slow_threshold_summary() == expected


@pytest.fixture(params=["memory", "sqlite"])
def make_store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[type]:
    created: list[StatsStore] = []

    def build(config: StatsConfig) -> StatsStore:
        cfg = StatsConfig(
            slow_threshold_ms=config.slow_threshold_ms,
            slow_thresholds=dict(config.slow_thresholds),
            sqlite_path=str(tmp_path / "t.db"),
        )
        store: StatsStore = MemoryStore(cfg) if request.param == "memory" else SqliteStore(cfg)
        created.append(store)
        return store

    yield build  # type: ignore[misc]
    for s in created:
        s.close()


def slow_keys(store: StatsStore) -> list[str]:
    return [s.key for s in store.slow_calls()]


def test_stores_use_overrides(make_store) -> None:
    config = StatsConfig(
        slow_threshold_ms=500, slow_thresholds={"/reports/{report_id}": 3000.0, LOOKUP: 20.0, "GET /off": None}
    )
    store = make_store(config)
    store.record(make_call(REPORT, duration_ms=2000))  # below its 3000 override, above the global 500
    store.record(make_call(LOOKUP, duration_ms=25))  # above its 20 override, below the global 500
    store.record(make_call("GET /off", duration_ms=9999))  # disabled for this route
    store.record(make_call("GET /plain", duration_ms=600))  # global threshold
    store.record(make_call(REPORT, duration_ms=3500))
    assert sorted(slow_keys(store)) == sorted([LOOKUP, "GET /plain", REPORT])


def test_global_none_still_logs_overridden_route(make_store) -> None:
    store = make_store(StatsConfig(slow_threshold_ms=None, slow_thresholds={REPORT: 100.0}))
    store.record(make_call(REPORT, duration_ms=150))
    store.record(make_call("GET /plain", duration_ms=9999))
    assert slow_keys(store) == [REPORT]


def test_heading_and_json_show_overrides() -> None:
    collector = StatsCollector(StatsConfig(slow_thresholds={"/a": 5.0, "GET /b": None}))
    assert "Slow calls (&gt;= 500 ms, 2 route overrides)" in render_html(collector)
    assert render_json(collector)["slow_thresholds"] == {"/a": 5.0, "GET /b": None}


def test_heading_without_overrides_is_unchanged() -> None:
    html = render_html(StatsCollector(StatsConfig()))
    assert "Slow calls (&gt;= 500 ms)" in html
    assert render_json(StatsCollector(StatsConfig()))["slow_thresholds"] == {}
