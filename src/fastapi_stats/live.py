"""Live (auto-refreshing) stats page (owned by #16)."""

from __future__ import annotations

from fastapi_stats.ext.base import NoopExtension, PagePart

PART = PagePart(name="live", html_top=lambda collector: "")
EXT = NoopExtension("live")
