"""Access control for the stats endpoints (owned by #9)."""

from __future__ import annotations

import base64
import binascii
import contextlib
import inspect
import secrets

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from fastapi_perfstats.config import StatsConfig


def _basic_auth_denied(request: Request, config: StatsConfig) -> Response | None:
    auth = config.credentials
    if auth is None:
        return None
    user, pw = b"", b""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "basic":
        with contextlib.suppress(binascii.Error, ValueError):
            user, _, pw = base64.b64decode(token, validate=True).partition(b":")
    # both compares always run (no short-circuit) so timing does not reveal which part was wrong
    ok_user = secrets.compare_digest(user, auth.username.encode())
    ok_pw = secrets.compare_digest(pw, auth.password.encode())
    if ok_user and ok_pw:
        return None
    return PlainTextResponse("Unauthorized", 401, headers={"WWW-Authenticate": 'Basic realm="stats"'})


async def authorize(request: Request, config: StatsConfig) -> Response | None:
    """``None`` if the request may pass, else the 401 / 403 response to return.

    Order: HTTP Basic (``config.basic_auth``, 401 on failure), then ``config.access`` (sync or async; a falsy result
    gives 403). Exceptions from ``access`` propagate and are never swallowed.
    """
    denied = _basic_auth_denied(request, config)
    if denied is not None:
        return denied
    if config.access is None:
        return None
    allowed = config.access(request)
    if inspect.isawaitable(allowed):
        allowed = await allowed
    if allowed:
        return None
    return PlainTextResponse("Forbidden", 403)


def localhost_only(request: Request) -> bool:
    """``AccessCheck`` allowing loopback clients only (``127.0.0.1`` and ``::1``).

    Unreliable behind a reverse proxy: the client address is then the proxy's.
    """
    client = request.client
    return client is not None and client.host in {"127.0.0.1", "::1"}
