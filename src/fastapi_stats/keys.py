"""Key building for a finished call (owned by #5)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi_stats.config import StatsConfig


def build_key(scope: Mapping[str, Any], config: StatsConfig, prefix: str) -> str | None:
    """Default key ``METHOD + prefix + route template``; the raw path when no route matched. ``None`` skips the call."""
    if config.key_func is not None:
        key = config.key_func(scope)
    else:
        route = scope.get("route")  # set by routing, i.e. only valid *after* the app has run
        template = getattr(route, "path", None)
        path = prefix + template if template else scope.get("path", "")
        key = f"{scope.get('method', 'GET')} {path}"
    return None if key is None else key[: config.max_key_length]
