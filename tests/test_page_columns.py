import re

import pytest

from fastapi_stats import StatsCollector, StatsConfig, render_html, render_json
from fastapi_stats.page import split_key


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("GET /items/{id}", ("GET", "/items/{id}")),
        ("POST /a b", ("POST", "/a b")),
        ("other", ("", "other")),
        ("get /x", ("", "get /x")),
        ("my tracked name", ("", "my tracked name")),
        ("", ("", "")),
    ],
)
def test_split_key(key, expected):
    assert split_key(key) == expected


def make():
    c = StatsCollector(StatsConfig(slow_threshold_ms=0))
    c.store.record("GET /items/{id}", 5, 200)
    c.store.record("POST /items", 7, 500)
    c.store.record("other", 1, 200)
    return c


def test_html_has_method_and_route_columns_in_all_three_tables():
    html = render_html(make())
    heads = re.findall(r"<thead><tr>(.*?)</tr></thead>", html)
    assert len(heads) == 3
    for h in heads:
        cols = re.findall(r"<th>(.*?)</th>", h)
        assert cols[:2] == ["Method", "Route"]
    assert "<td>GET</td><td>/items/{id}</td>" in html
    assert "<td>POST</td><td>/items</td>" in html
    assert "<td></td><td>other</td>" in html


def test_html_escapes_method_and_route_cells():
    c = StatsCollector()
    c.store.record("GET <b>x</b>", 1, 200)
    c.store.record("PO<i>ST /y", 1, 200)  # not a plain method -> whole key is the route, still escaped
    html = render_html(c)
    assert "<b>x</b>" not in html and "<i>" not in html
    assert "<td>GET</td><td>&lt;b&gt;x&lt;/b&gt;</td>" in html


def test_json_has_separate_fields_and_keeps_key():
    j = render_json(make())
    r = next(x for x in j["routes"] if x["key"] == "GET /items/{id}")
    assert (r["method"], r["route"]) == ("GET", "/items/{id}")
    assert all("method" in s and "route" in s for s in j["slow_calls"])
    o = next(x for x in j["routes"] if x["key"] == "other")
    assert (o["method"], o["route"]) == ("", "other")
