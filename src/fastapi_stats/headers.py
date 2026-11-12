"""Response headers of the stats endpoints (owned by #8)."""

from __future__ import annotations


def page_headers(css: str, js: str) -> dict[str, str]:
    """Headers for the HTML page, given its inline CSS and JS. Stub: none."""
    return {}


def data_headers() -> dict[str, str]:
    """Headers for the JSON / text endpoints. Stub: none."""
    return {}
