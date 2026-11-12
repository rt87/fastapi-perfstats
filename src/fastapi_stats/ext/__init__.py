"""Registry of statistics extensions, in fixed order."""

from __future__ import annotations

from fastapi_stats import live, slow_context
from fastapi_stats.ext import recent, sizes, stddev
from fastapi_stats.ext.base import Extension

EXTENSIONS: tuple[Extension, ...] = (stddev.EXT, sizes.EXT, recent.EXT, slow_context.EXT, live.EXT)
