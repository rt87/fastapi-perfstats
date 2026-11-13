"""Error rates and stddev extension: both stores give the same numbers; columns are sortable and escaped."""

import math
from collections.abc import Iterator
from pathlib import Path

import pytest

from fakes import make_call, make_route_stats
from fastapi_perfstats import StatsCollector, StatsConfig, render_html
from fastapi_perfstats.ext import stddev
from fastapi_perfstats.models import RouteStats
from fastapi_perfstats.stores import MemoryStore, SqliteStore, StatsStore

KEY = "GET /items/{item_id}"


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StatsStore]:
    config = StatsConfig(sqlite_path=str(tmp_path / "s.db"))
    s: StatsStore = MemoryStore(config) if request.param == "memory" else SqliteStore(config)
    yield s
    s.close()


def only(store: StatsStore, key: str = KEY) -> RouteStats:
    return next(r for r in store.routes() if r.key == key)


def test_known_values(store: StatsStore) -> None:
    for ms, status in [(10, 200), (20, 200), (30, 500), (40, 500)]:
        store.record(make_call(duration_ms=ms, status=status))
    row = only(store)
    assert row.error_rate_5xx == 0.5
    assert row.error_rate_4xx == 0.0
    assert row.stddev_ms == pytest.approx(math.sqrt(125.0))


def test_three_durations_stddev(store: StatsStore) -> None:
    for ms in (10, 20, 30):
        store.record(make_call(duration_ms=ms))
    assert only(store).stddev_ms == pytest.approx(8.165, abs=1e-3)


def test_4xx_rate(store: StatsStore) -> None:
    for status in (200, 404, 404, 200):
        store.record(make_call(status=status))
    row = only(store)
    assert row.error_rate_4xx == 0.5 and row.error_rate_5xx == 0.0


def test_single_call_has_zero_stddev(store: StatsStore) -> None:
    store.record(make_call(duration_ms=123.456))
    assert only(store).stddev_ms == pytest.approx(0.0, abs=1e-6)


def test_zero_errors_gives_zero_rate(store: StatsStore) -> None:
    store.record(make_call())
    row = only(store)
    assert row.error_rate_4xx == 0.0 and row.error_rate_5xx == 0.0


def test_no_calls_no_routes(store: StatsStore) -> None:
    assert store.routes() == []


def test_stores_agree(tmp_path: Path) -> None:
    config = StatsConfig(sqlite_path=str(tmp_path / "x.db"))
    memory, sqlite = MemoryStore(config), SqliteStore(config)
    for i in range(20):
        call = make_call(duration_ms=i * 1.5 + 1, status=500 if i % 4 == 0 else 200)
        memory.record(call)
        sqlite.record(call)
    m, s = only(memory), only(sqlite)
    sqlite.close()
    assert (m.error_rate_4xx, m.error_rate_5xx) == (s.error_rate_4xx, s.error_rate_5xx)
    assert m.stddev_ms == pytest.approx(s.stddev_ms)


def test_reset_clears_state(store: StatsStore) -> None:
    store.record(make_call(duration_ms=50))
    store.reset()
    store.record(make_call(duration_ms=10))
    assert only(store).stddev_ms == pytest.approx(0.0, abs=1e-6)


def test_sqlite_missing_ext_row_leaves_none(tmp_path: Path) -> None:
    store = SqliteStore(StatsConfig(sqlite_path=str(tmp_path / "m.db")), extensions=[])
    store.record(make_call())
    rows = {r.key: r for r in store.routes()}
    store.close()
    assert rows[KEY].stddev_ms is None


def test_extension_none_when_route_has_no_state() -> None:
    row = make_route_stats(count=3, errors_5xx=3)
    stddev.EXT.memory_finish(row, {}, StatsConfig(), 0.0)
    assert row.stddev_ms is None and row.error_rate_5xx == 1.0


def test_rounding_never_gives_negative_variance() -> None:
    row = make_route_stats(count=3, mean_ms=0.1)
    stddev.EXT.memory_finish(row, {"total_sq": 3 * 0.1**2 - 1e-12}, StatsConfig(), 0.0)
    assert row.stddev_ms == 0.0


def test_zero_count_rates() -> None:
    row = make_route_stats(count=0)
    stddev.EXT.memory_finish(row, {}, StatsConfig(), 0.0)
    assert row.error_rate_4xx == 0.0 and row.stddev_ms is None


def test_columns_render_sortable_values() -> None:
    collector = StatsCollector(StatsConfig())
    for ms, status in [(10, 200), (30, 500)]:
        collector.store.record(make_call(duration_ms=ms, status=status))
    html = render_html(collector)
    freq = html.split('id="freq"')[1].split("</table>")[0]
    assert "<th>5xx %</th>" in freq and "<th>4xx %</th>" in freq
    assert '<td data-v="0.5">50.0%</td>' in freq
    rt = html.split('id="rt"')[1].split("</table>")[0]
    assert "<th>Stddev</th>" in rt and '<td data-v="10.0">10.00</td>' in rt


def test_stddev_none_renders_na_with_sortable_value() -> None:
    config = StatsConfig()
    column = next(c for c in stddev.EXT.columns(config) if c.header == "Stddev")
    assert column.cell(make_route_stats(stddev_ms=None), config) == ("n/a", -1.0)


def test_columns_are_plain_text_so_the_page_escapes_them() -> None:
    assert not any(c.raw_html for c in stddev.EXT.columns(StatsConfig()))
