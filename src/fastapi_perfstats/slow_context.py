"""Extra detail captured with slow calls: redacted query string, request / trace id and a custom hook.

Everything here is opt-in or privacy-safe: the query is only captured when ``slow_capture_query`` is on and
sensitive parameter values are redacted; only the configured id headers are read; bodies and other headers never are.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode

from fastapi_perfstats.config import StatsConfig
from fastapi_perfstats.ext.base import Column, NoopExtension
from fastapi_perfstats.models import SlowCall, SlowExtra

log = logging.getLogger("fastapi_perfstats")

REDACTED = "[redacted]"
MAX_QUERY_LENGTH = 500
MAX_ID_LENGTH = 128
MAX_CONTEXT_ITEMS = 10
MAX_CONTEXT_TEXT = 200

_TRACE_ID = re.compile(r"[0-9a-fA-F]{32}")
_logged_hook_errors: set[str] = set()


def redact_query(raw: str, fragments: tuple[str, ...]) -> str | None:
    """Redact values whose parameter NAME contains any of ``fragments`` (case-insensitive); ``None`` if empty."""
    pairs = parse_qsl(raw, keep_blank_values=True)
    if not pairs:
        return None
    lowered = tuple(f.lower() for f in fragments)
    cleaned = [(k, REDACTED if any(f in k.lower() for f in lowered) else v) for k, v in pairs]
    return urlencode(cleaned)[:MAX_QUERY_LENGTH]


def _header_value(scope: Mapping[str, Any], name: str) -> str | None:
    for raw_name, raw_value in scope.get("headers") or ():
        if raw_name.decode("latin-1").lower() == name:
            return str(raw_value.decode("latin-1")).strip()
    return None


def _request_id(scope: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    """First usable header wins; ``traceparent`` contributes only its 32-hex trace id."""
    for name in names:
        value = _header_value(scope, name)
        if not value:
            continue
        if name == "traceparent":
            parts = value.split("-")
            if len(parts) < 2 or not _TRACE_ID.fullmatch(parts[1]):
                continue
            value = parts[1]
        return value[:MAX_ID_LENGTH]
    return None


def _context(scope: Mapping[str, Any], config: StatsConfig) -> dict[str, str] | None:
    hook = config.slow_context
    if hook is None:
        return None
    try:
        result = hook(scope)
        return {
            str(k)[:MAX_CONTEXT_TEXT]: str(v)[:MAX_CONTEXT_TEXT] for k, v in list(result.items())[:MAX_CONTEXT_ITEMS]
        }
    except Exception as exc:
        name = type(exc).__name__
        if name not in _logged_hook_errors:
            _logged_hook_errors.add(name)
            log.warning("slow_context hook raised %s; further errors of this type are not logged", name, exc_info=True)
        return {"context_error": name}


def capture(scope: Mapping[str, Any], config: StatsConfig, key: str, duration_ms: float) -> SlowExtra:
    """Query / request id / context for a slow call. Never raises (the hook's errors are contained)."""
    query = None
    if config.slow_capture_query:
        raw = scope.get("query_string") or b""
        query = redact_query(raw.decode("utf-8", errors="replace"), config.redact_query_params)
    return SlowExtra(
        query=query,
        request_id=_request_id(scope, config.slow_request_id_headers),
        context=_context(scope, config),
    )


def _text_cell(attr: str) -> Any:
    def cell(call: SlowCall, config: StatsConfig) -> tuple[str, float | None]:
        return getattr(call, attr) or "", None

    return cell


def _context_cell(call: SlowCall, config: StatsConfig) -> tuple[str, float | None]:
    return "; ".join(f"{k}={v}" for k, v in call.context.items()), None


class SlowContextExtension(NoopExtension):
    """Only contributes the slow-table columns; persistence of the extras is done by the stores."""

    def __init__(self) -> None:
        super().__init__("slow_context")

    def columns(self, config: StatsConfig) -> tuple[Column, ...]:
        return (
            Column("slow", "Request id", 60, _text_cell("request_id")),
            Column("slow", "Query", 70, _text_cell("query")),
            Column("slow", "Context", 80, _context_cell),
        )


EXT = SlowContextExtension()
