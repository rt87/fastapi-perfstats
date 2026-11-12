"""Recent activity: per-minute buckets, windows, series, edge cases, both stores, columns."""

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from fakes import make_call, make_route_stats
from fastapi_stats import StatsCollector, StatsConfig, render_html
from fastapi_stats.ext import recent
from fastapi_stats.ext.recent import Bucket, recent_stats
from fastapi_stats.models import RouteStats
from fastapi_stats.stores import MemoryStore, SqliteStore, StatsStore

KEY = "GET /items/{item_id}"
NOW = int(time.time() // 60) * 60 + 30.0  # 30 s into the current minute (the SQLite writer prunes by the real clock)
NOW_MINUTE = int(NOW // 60)


def minute_ts(ago: int, offset: float = 10.0) -> float:
    return (NOW_MINUTE - ago) * 60 + offset


def make_store(kind: str, tmp_path: Path, **config: object) -> StatsStore:
    cfg = StatsConfig(sqlite_path=str(tmp_path / "r.db"), **config)  # type: ignore[arg-type]
    return MemoryStore(cfg) if kind == "memory" else SqliteStore(cfg)


@pytest.fixture(params=["memory", "sqlite"])
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def store(kind: str, tmp_path: Path) -> Iterator[StatsStore]:
    s = make_store(kind, tmp_path)
    yield s
    s.close()


def finish(store: StatsStore, now: float = NOW) -> RouteStats:
    """Read one route through the extension hooks with an explicit clock."""
    if isinstance(store, MemoryStore):
        state = store._aggs[KEY]
        row = RouteStats(KEY, state.count, state.e4, state.e5, 0, 0, 0, first_ts=state.first)
        recent.EXT.memory_finish(row, state.ext["recent"], store.cfg, now)
        return row
    assert isinstance(store, SqliteStore)
    store.flush()
    row = RouteStats(KEY, 1, 0, 0, 0, 0, 0, first_ts=minute_ts(100))
    recent.EXT.sqlite_finish({KEY: row}, store._db, store.cfg, now)
    return row


# -- recent_stats (pure) ---------------------------------------------------------------------------------------


def test_recent_stats_windows_and_values() -> None:
    buckets = [Bucket(NOW_MINUTE - 10, 60, 6, 600.0), Bucket(NOW_MINUTE, 30, 3, 90.0)]
    five = recent_stats(buckets, NOW, 5, first_ts=NOW - 7200)
    assert (five.calls, five.errors_5xx) == (30, 3)
    assert five.error_rate_5xx == pytest.approx(0.1)
    assert five.mean_ms == pytest.approx(3.0)
    assert five.calls_per_min == pytest.approx(30 / (4 + 0.5))
    fifteen = recent_stats(buckets, NOW, 15, first_ts=NOW - 7200)
    assert fifteen.calls == 90


def test_recent_stats_window_edges_are_exclusive_inclusive() -> None:
    buckets = [Bucket(NOW_MINUTE - 5, 7, 0, 7.0), Bucket(NOW_MINUTE - 4, 1, 0, 1.0), Bucket(NOW_MINUTE + 1, 9, 0, 9.0)]
    assert recent_stats(buckets, NOW, 5, first_ts=0).calls == 1  # minute-5 is outside, the future is ignored


def test_recent_stats_young_route_uses_age_and_minimum_span() -> None:
    buckets = [Bucket(NOW_MINUTE, 6, 0, 6.0)]
    assert recent_stats(buckets, NOW, 15, first_ts=NOW - 30).calls_per_min == pytest.approx(6 / 0.5)
    assert recent_stats(buckets, NOW, 15, first_ts=NOW).calls_per_min == pytest.approx(6 * 60)  # span floor 1 s


def test_recent_stats_empty_has_zero_rates() -> None:
    stats = recent_stats([], NOW, 5, first_ts=0)
    assert (stats.calls, stats.calls_per_min, stats.error_rate_5xx, stats.mean_ms) == (0, 0.0, 0.0, 0.0)


# -- both stores -------------------------------------------------------------------------------------------------


def fill(store: StatsStore) -> None:
    for _ in range(60):
        store.record(make_call(ts=minute_ts(10)))
    for i in range(30):
        store.record(make_call(ts=minute_ts(0), status=500 if i < 3 else 200, duration_ms=3.0))


def test_acceptance_windows(store: StatsStore) -> None:
    fill(store)
    row = finish(store)
    assert row.recent["5m"].calls == 30
    assert row.recent["15m"].calls == 90
    assert row.recent["1m"].calls == 30
    assert row.recent["5m"].errors_5xx == 3


def test_series_is_zero_filled_with_fixed_length(store: StatsStore) -> None:
    fill(store)
    series = finish(store).series
    assert len(series) == 60
    assert series[-1] == 30 and series[-11] == 60
    assert sum(series) == 90


def test_gap_longer_than_window_gives_all_zeros(store: StatsStore) -> None:
    fill(store)
    row = finish(store, now=NOW + 3 * 3600)
    assert row.series == [0] * 60
    assert all(s.calls == 0 for s in row.recent.values())


def test_backwards_timestamps_do_not_corrupt_or_raise(store: StatsStore) -> None:
    store.record(make_call(ts=minute_ts(0)))
    store.record(make_call(ts=minute_ts(5)))  # clock went backwards
    store.record(make_call(ts=minute_ts(0, 20.0)))
    row = finish(store)
    assert row.series[-1] == 3 and sum(row.series) == 3
    assert row.recent["1m"].calls == 3


def test_memory_and_sqlite_agree(tmp_path: Path) -> None:
    memory, sqlite = make_store("memory", tmp_path), make_store("sqlite", tmp_path)
    for s in (memory, sqlite):
        fill(s)
        s.record(make_call(ts=minute_ts(20)))
        s.record(make_call(ts=minute_ts(25)))  # backwards
    m, q = finish(memory), finish(sqlite)
    sqlite.close()
    assert m.recent["15m"].calls == q.recent["15m"].calls
    assert m.series == q.series


def test_disabled_keeps_nothing(kind: str, tmp_path: Path) -> None:
    store = make_store(kind, tmp_path, recent_minutes=0)
    store.record(make_call(ts=minute_ts(0)))
    row = next(r for r in store.routes() if r.key == KEY)
    assert row.recent == {} and row.series == []
    if isinstance(store, SqliteStore):
        assert store._db.execute("SELECT COUNT(*) FROM ext_recent").fetchone()[0] == 0
    elif isinstance(store, MemoryStore):
        assert not store._aggs[KEY].ext["recent"]["buckets"]
    store.close()


def test_sqlite_rows_stay_bounded(tmp_path: Path) -> None:
    store = make_store("sqlite", tmp_path, recent_minutes=5, recent_windows=(1, 5))
    assert isinstance(store, SqliteStore)
    base = int(time.time() // 60)
    for i in range(30):  # 30 consecutive minutes ending now
        store.record(make_call(ts=(base - 29 + i) * 60 + 1.0))
    store.flush()
    minutes = [r[0] for r in store._db.execute("SELECT minute FROM ext_recent")]
    assert len(minutes) <= 5 and min(minutes) > base - 5
    store.close()


def test_memory_buckets_trimmed_to_recent_minutes(tmp_path: Path) -> None:
    store = make_store("memory", tmp_path, recent_minutes=3, recent_windows=(1, 3))
    for i in range(10):
        store.record(make_call(ts=(NOW_MINUTE - 9 + i) * 60 + 1.0))
    row = finish(store)
    assert len(row.series) == 3 and row.series == [1, 1, 1]
    assert len(store._aggs[KEY].ext["recent"]["buckets"]) == 3  # type: ignore[attr-defined]


def test_reset_clears_buckets(store: StatsStore) -> None:
    store.record(make_call(ts=minute_ts(0)))
    store.reset()
    store.record(make_call(ts=minute_ts(0)))
    assert finish(store).series[-1] == 1


def test_routes_integration_uses_real_clock(kind: str, tmp_path: Path) -> None:
    store = make_store(kind, tmp_path)
    now = time.time()
    for _ in range(4):
        store.record(make_call(ts=now))
    row = next(r for r in store.routes() if r.key == KEY)
    assert row.recent["1m"].calls == 4 and sum(row.series) == 4
    store.close()


# -- config validation ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [{"recent_windows": (0,)}, {"recent_windows": (61,)}, {"recent_minutes": 1441}, {"recent_minutes": -1}]
)
def test_invalid_windows_raise(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="recent_"):
        StatsConfig(**bad)  # type: ignore[arg-type]


def test_windows_sorted_and_unique() -> None:
    assert StatsConfig(recent_windows=(15, 1, 5, 5)).recent_windows == (1, 5, 15)


# -- hooks / columns -----------------------------------------------------------------------------------------------


def test_sqlite_finish_without_buckets_gives_zero_series() -> None:
    db = sqlite3.connect(":memory:")
    db.execute(recent._SCHEMA)
    row = make_route_stats()
    recent.EXT.sqlite_finish({row.key: row}, db, StatsConfig(), NOW)
    assert row.series == [0] * 60 and row.recent["5m"].calls == 0
    db.close()


def test_memory_finish_without_state_leaves_row_alone() -> None:
    row = make_route_stats()
    recent.EXT.memory_finish(row, {}, StatsConfig(), NOW)
    assert row.recent == {} and row.series == []


def test_columns_for_windows_and_biggest_error_rate() -> None:
    cols = recent.EXT.columns(StatsConfig(recent_windows=(1, 5, 15)))
    assert [c.header for c in cols] == ["Calls/min (1m)", "Calls/min (5m)", "Calls/min (15m)", "5xx % (15m)"]
    assert [c.order for c in cols] == [91, 92, 93, 99]
    assert all(c.table == "freq" for c in cols)


def test_no_columns_when_disabled_or_no_windows() -> None:
    assert recent.EXT.columns(StatsConfig(recent_minutes=0)) == ()
    assert recent.EXT.columns(StatsConfig(recent_windows=())) == ()


def test_many_windows_stay_within_order_range() -> None:
    cols = recent.EXT.columns(StatsConfig(recent_minutes=60, recent_windows=tuple(range(1, 13))))
    assert max(c.order for c in cols) == 99 and min(c.order for c in cols) == 91


def test_cells_format_and_missing_window() -> None:
    config = StatsConfig()
    rate, err = recent.EXT.columns(config)[1], recent.EXT.columns(config)[-1]
    stats = recent_stats([Bucket(NOW_MINUTE, 3, 1, 9.0)], NOW, 5, first_ts=NOW - 60)
    row = make_route_stats(recent={"5m": stats, "15m": stats})
    assert rate.cell(row, config)[0] == f"{stats.calls_per_min:.2f}"
    text, value = err.cell(row, config)
    assert text == "33.3%" and value == pytest.approx(1 / 3)
    bare = make_route_stats()
    assert rate.cell(bare, config) == ("n/a", -1.0) and err.cell(bare, config) == ("n/a", -1.0)


def test_page_shows_columns_and_all_header() -> None:
    collector = StatsCollector(StatsConfig())
    collector.store.record(make_call(ts=time.time()))
    html = render_html(collector)
    freq = html.split('id="freq"')[1].split("</table>")[0]
    for header in ("Calls/min (all)", "Calls/min (1m)", "Calls/min (15m)", "5xx % (15m)"):
        assert f"<th>{header}</th>" in freq
    off = render_html(StatsCollector(StatsConfig(recent_minutes=0)))
    assert "(1m)" not in off and "<th>Calls/min</th>" in off
