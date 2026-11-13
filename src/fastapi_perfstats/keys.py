"""Key building for a finished call (owned by #5)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi_perfstats.config import StatsConfig
from fastapi_perfstats.models import KNOWN_METHODS, OTHER_METHOD, UNMATCHED_ROUTE


def build_key(scope: Mapping[str, Any], config: StatsConfig, prefix: str) -> str | None:
    """Default key ``METHOD + prefix + route template``; ``None`` skips the call.

    Requests without a matching route follow ``config.unmatched``: ``"group"`` -> one key ``METHOD <unmatched>``,
    ``"raw"`` -> the raw path, ``"skip"`` -> ``None``. Unknown HTTP methods are keyed as ``OTHER``.
    """
    if config.key_func is not None:
        key = config.key_func(scope)
    else:
        raw_method = scope.get("method", "GET")
        method = raw_method if raw_method in KNOWN_METHODS else OTHER_METHOD
        route = scope.get("route")  # set by routing, i.e. only valid *after* the app has run
        template = getattr(route, "path", None)
        if template:
            path = prefix + template
        elif config.unmatched == "skip":
            return None
        elif config.unmatched == "group":
            path = UNMATCHED_ROUTE
        else:
            path = scope.get("path", "")
        key = f"{method} {path}"
    return None if key is None else key[: config.max_key_length]
