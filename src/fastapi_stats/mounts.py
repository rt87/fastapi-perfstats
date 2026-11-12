"""Mounted sub-application support (owned by #4)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from starlette.routing import Mount


def mount_prefix(scope: Mapping[str, Any]) -> str:
    """Path prefix added by ``Mount()``s: ``root_path`` minus the app's own root path, ``""`` when not mounted.

    Starlette's ``Mount.matches`` (checked on 0.37 and 1.x) puts the root path seen *before* the first mount into
    ``scope["app_root_path"]`` once and extends ``scope["root_path"]`` by each matched mount path. A proxy root path
    (``--root-path``) is part of both and therefore cancels out.

    When no inner route matched (``app.mount("/static", asgi_app)``), ``scope["route"]`` is the ``Mount`` itself and
    its path is already the key template, so its own part is not repeated in the prefix.

    Limitation: a mount path with parameters (``/tenants/{tenant}``) contributes the raw value, as the scope only
    carries the matched path, not the mount template.
    """
    if "app_root_path" not in scope:
        return ""
    root_path = str(scope.get("root_path", ""))
    app_root_path = str(scope["app_root_path"])
    if not root_path.startswith(app_root_path):
        return ""
    prefix = root_path[len(app_root_path) :]
    route = scope.get("route")
    if isinstance(route, Mount) and route.path and prefix.endswith(route.path):
        prefix = prefix[: -len(route.path)]
    return prefix
