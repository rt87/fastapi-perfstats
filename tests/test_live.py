import re

import pytest

from fakes import RecordingStore, make_call, make_route_stats
from fastapi_stats import StatsCollector, StatsConfig, live, render_html
from fastapi_stats.page import script_text


def point_list(svg):
    match = re.search(r'points="([^"]+)"', svg)
    assert match
    return match[1].split()


QS = {label: 1.0 for label in ("p50", "p90", "p95", "p99")}


class SeriesStore(RecordingStore):
    def __init__(self, *rows):
        super().__init__()
        self.rows = list(rows)

    def routes(self):
        return self.rows


def collector(**config):
    c = StatsCollector(StatsConfig(**config))
    c.store.record(make_call("GET /a", 5, 200))
    return c


def test_selector_is_always_rendered_and_off_by_default():
    html = render_html(collector())
    assert 'id="refresh"' in html
    assert 'data-refresh="0"' in html
    assert re.findall(r'<option value="(\d+)" selected>', html) == ["0"]


@pytest.mark.parametrize("seconds", [0, 5, 15, 60])
def test_selector_preselects_configured_value(seconds):
    html = render_html(collector(refresh_seconds=seconds))
    assert f'data-refresh="{seconds}"' in html
    selected = re.findall(r'<option value="(\d+)" selected>', html)
    assert selected == [str(seconds)]
    assert len(re.findall(r"<option ", html)) == 4
    assert 'id="refresh-note"' in html


@pytest.mark.parametrize("bad", [1, 30, -5, 120])
def test_invalid_refresh_seconds_raises(bad):
    with pytest.raises(ValueError, match="refresh_seconds"):
        StatsConfig(refresh_seconds=bad)


def test_script_contains_refresh_logic_without_inline_handlers():
    js = script_text(collector())
    assert "DOMParser" in js and "no-store" in js and "refresh failed" in js
    html = render_html(collector(refresh_seconds=5))
    assert " onclick=" not in html and " onchange=" not in html and " style=" not in html


def test_sparkline_points_and_label():
    svg = live.sparkline([0, 2, 4, 0], 4)
    assert svg.count("<svg") == 1 and svg.count("<polyline") == 1
    points = point_list(svg)
    assert len(points) == 4
    assert 'aria-label="calls per minute, last 4 min, max 4"' in svg
    assert 'stroke="currentColor"' in svg and 'fill="none"' in svg
    assert 'width="120" height="20" viewBox="0 0 120 20" role="img"' in svg
    ys = [float(p.split(",")[1]) for p in points]
    assert ys[2] < ys[1] < ys[0] and ys[3] == ys[0]  # taller value, smaller y


def test_sparkline_flat_for_all_zero_and_empty_and_single():
    zeros = live.sparkline([0, 0, 0], 3)
    ys = {p.split(",")[1] for p in point_list(zeros)}
    assert len(ys) == 1
    empty = live.sparkline([], 3)
    assert len(point_list(empty)) == 2
    single = live.sparkline([7], 1)
    assert len(point_list(single)) == 1 and "max 7" in single


def test_column_rendered_per_route_with_one_point_per_minute():
    c = StatsCollector(StatsConfig(recent_minutes=5, recent_windows=(1,)))
    c.store = SeriesStore(
        make_route_stats("GET /a", series=[0, 1, 2, 3, 4], quantiles=QS),
        make_route_stats("GET /b", series=[0] * 5, quantiles=QS),
    )
    html = render_html(c)
    assert "<th>Last 5 min</th>" in html
    svgs = re.findall(r"<svg .*?</svg>", html)
    assert len(svgs) == 2
    for svg in svgs:
        assert len(point_list(svg)) == 5


def test_no_sparkline_column_when_recent_minutes_is_zero():
    html = render_html(collector(recent_minutes=0, recent_windows=()))
    assert "<svg" not in html and "Last " not in html.split("<h2>")[1]
    assert live.EXT.columns(StatsConfig(recent_minutes=0, recent_windows=())) == ()


def test_column_cell_is_raw_and_sorts_by_total():
    (col,) = live.EXT.columns(StatsConfig(recent_minutes=3, recent_windows=(1,)))
    assert col.raw_html and col.table == "freq" and col.order == 25
    text, value = col.cell(make_route_stats(series=[1, 2, 3]), StatsConfig(recent_minutes=3, recent_windows=(1,)))
    assert text.startswith("<svg") and value == 6.0


def test_route_key_is_still_escaped_next_to_raw_column():
    c = StatsCollector(StatsConfig(recent_minutes=2, recent_windows=(1,)))
    c.store = SeriesStore(make_route_stats("GET <script>x</script>", series=[1, 2], quantiles=QS))
    html = render_html(c)
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html
