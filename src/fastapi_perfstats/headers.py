"""Response headers of the stats endpoints (owned by #8)."""

from __future__ import annotations

import base64
import hashlib


def csp_hash(text: str) -> str:
    """CSP source expression (``'sha256-...'``) for an inline ``<style>`` / ``<script>`` body, exactly as served."""
    digest = hashlib.sha256(text.encode()).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def page_headers(css: str, js: str) -> dict[str, str]:
    """Headers for the HTML page, given the exact inline CSS and JS between its tags (hashes computed per call).

    Rule for the page markup: no inline event handlers, no ``style="..."`` attributes, no dynamic content inside
    ``<script>`` / ``<style>``; pass data via ``data-*`` attributes.
    """
    policy = (
        f"default-src 'none'; style-src {csp_hash(css)}; script-src {csp_hash(js)}; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    return {
        "Content-Security-Policy": policy,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Cache-Control": "no-store",
    }


def data_headers() -> dict[str, str]:
    """Headers for the JSON / text endpoints and the 401/403 responses."""
    return {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
