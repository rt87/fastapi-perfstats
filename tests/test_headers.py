"""Security headers and CSP hashes of the stats endpoints (#8)."""

import base64
import hashlib
import re

from starlette.applications import Starlette
from starlette.testclient import TestClient

from fastapi_perfstats import StatsCollector, StatsConfig, mount_stats
from fastapi_perfstats.headers import csp_hash, data_headers, page_headers

AUTH = ("admin", "change-me")


def client(config: StatsConfig | None = None) -> TestClient:
    collector = StatsCollector(config or StatsConfig(basic_auth=AUTH))
    app = Starlette()
    mount_stats(app, collector)
    return TestClient(app)


def sha(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def test_page_headers_contain_all_security_headers() -> None:
    h = page_headers("a{}", "var x;")
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Referrer-Policy"] == "no-referrer"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Cache-Control"] == "no-store"
    csp = h["Content-Security-Policy"]
    assert csp.startswith("default-src 'none';")
    assert f"style-src {sha('a{}')};" in csp
    assert f"script-src {sha('var x;')};" in csp
    for directive in ("img-src 'self' data:", "connect-src 'self'", "base-uri 'none'", "frame-ancestors 'none'"):
        assert directive in csp


def test_data_headers() -> None:
    assert data_headers() == {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def test_changed_inline_text_changes_the_hash() -> None:
    assert csp_hash("var a;") != csp_hash("var a; ")
    assert page_headers("c", "j1")["Content-Security-Policy"] != page_headers("c", "j2")["Content-Security-Policy"]


def test_served_page_carries_headers_and_hashes_match_inline_content() -> None:
    r = client().get("/stats", auth=AUTH)
    assert r.status_code == 200
    style = re.search(r"<style>(.*?)</style>", r.text, re.S)
    script = re.search(r"<script>(.*?)</script>", r.text, re.S)
    assert style is not None and script is not None
    csp = r.headers["content-security-policy"]
    assert f"style-src {sha(style.group(1))};" in csp
    assert f"script-src {sha(script.group(1))};" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_page_has_no_inline_style_or_event_handler_attributes() -> None:
    html = client().get("/stats", auth=AUTH).text
    body = re.sub(r"<(script|style)>.*?</\1>", "", html, flags=re.S)
    assert 'style="' not in body
    assert not re.search(r"\son[a-z]+\s*=", body, re.I)


def test_json_and_401_carry_no_store_and_nosniff() -> None:
    cl = client()
    for response in (cl.get("/stats.json", auth=AUTH), cl.get("/stats.json"), cl.get("/stats")):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
    assert cl.get("/stats").status_code == 401


def test_json_has_no_csp_header() -> None:
    assert "content-security-policy" not in client().get("/stats.json", auth=AUTH).headers
