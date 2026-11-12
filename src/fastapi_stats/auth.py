"""Access control for the stats endpoints (owned by #9)."""

from __future__ import annotations

import base64
import binascii
import contextlib
import secrets

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from fastapi_stats.config import StatsConfig


async def authorize(request: Request, config: StatsConfig) -> Response | None:
    """``None`` if the request may pass, else a 401 response (HTTP Basic, from ``config.basic_auth``)."""
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


def localhost_only(request: Request) -> bool:
    """``AccessCheck`` allowing loopback clients only. Not implemented yet (#9)."""
    raise NotImplementedError("localhost_only is implemented by #9")
