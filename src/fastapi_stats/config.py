"""Single configuration object for fastapi-stats."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from starlette.requests import Request

DEFAULT_QUANTILES: tuple[float, ...] = (0.5, 0.95, 0.99)

KeyFunc = Callable[[Mapping[str, Any]], "str | None"]
AccessCheck = Callable[[Request], "bool | Awaitable[bool]"]
SlowContextFunc = Callable[[Mapping[str, Any]], Mapping[str, str]]

_PROMETHEUS_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_REFRESH_CHOICES = (None, 0, 5, 15, 60)
_UNMATCHED_CHOICES = ("group", "raw", "skip")


@dataclass(frozen=True)
class BasicAuth:
    """HTTP Basic credentials for the stats page. The password is hidden from ``repr`` and never logged."""

    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class StatsConfig:
    """All knobs in one place.

    quantiles: percentiles shown/exported, values in (0, 1]. Custom values *replace* the defaults.
    storage: ``"memory"`` (default), ``"sqlite"`` or any object implementing ``StatsStore``.
    sqlite_path: database file used when ``storage="sqlite"``.
    window_size: durations kept per route (ring buffer); quantiles are computed on this window.
    max_routes: distinct keys kept; further new keys are merged into the ``"other"`` bucket.
    slow_threshold_ms: calls taking at least this long are logged individually; ``None`` disables the log.
    max_slow_calls: size of the slow-call log; when full the oldest entry is dropped (most recent N kept).
    stats_path: URL of the HTML page (JSON lives at ``stats_path + ".json"``).
    exclude_paths: raw request paths (and their sub-paths) that are never counted. ``stats_path`` and
        its JSON twin are always excluded.
    key_func: optional ``scope -> key`` (or ``None`` to skip the call), called after the request finished.
    basic_auth: ``(user, password)`` or ``BasicAuth``; protects the HTML page and the JSON endpoint
        (``secrets.compare_digest``, 401 + ``WWW-Authenticate``). ``None`` = no auth (a warning is logged on mount).
    max_key_length: keys longer than this are truncated (defence against huge raw paths).
    unmatched: how requests without a matching route are keyed: ``"group"``, ``"raw"`` or ``"skip"`` (#5).
    close_on_shutdown: close the store when the app shuts down (#7).
    access: optional extra access check ``request -> bool`` (may be async) for the stats endpoints (#9).
    allow_reset: enable the reset endpoint (#10).
    slow_thresholds: per-key slow thresholds in ms (``None`` = never slow); keys non-empty, values >= 0 (#12).
    slow_capture_query: store the (redacted) query string with slow calls (#13).
    redact_query_params: query parameter name fragments whose values are redacted; lower-cased (#13).
    slow_request_id_headers: request headers tried in order for a request id; lower-cased (#13).
    slow_context: optional ``scope -> {name: value}`` hook adding context to slow calls (#13).
    track_sizes: track request/response payload sizes (#6, #14).
    recent_minutes: minutes of recent activity kept, 0..1440; 0 disables (#15).
    recent_windows: recent windows in minutes, each 1..recent_minutes; sorted, unique (#15).
    refresh_seconds: live page refresh interval: ``None``, 0, 5, 15 or 60 (#16).
    prometheus: enable the Prometheus text endpoint (#17).
    prometheus_prefix: metric name prefix, ``[a-zA-Z_:][a-zA-Z0-9_:]*`` (#17).
    """

    quantiles: tuple[float, ...] = DEFAULT_QUANTILES
    storage: Any = "memory"
    sqlite_path: str = "fastapi_stats.db"
    window_size: int = 1000
    max_routes: int = 200
    slow_threshold_ms: float | None = 500.0
    max_slow_calls: int = 200
    stats_path: str = "/stats"
    exclude_paths: tuple[str, ...] = ("/health",)
    key_func: KeyFunc | None = None
    max_key_length: int = 200
    basic_auth: BasicAuth | tuple[str, str] | None = None
    unmatched: Literal["group", "raw", "skip"] = "group"
    close_on_shutdown: bool = True
    access: AccessCheck | None = None
    allow_reset: bool = False
    slow_thresholds: Mapping[str, float | None] = field(default_factory=dict)
    slow_capture_query: bool = False
    redact_query_params: tuple[str, ...] = ("pass", "tok", "secret", "key", "auth", "sig", "session", "code")
    slow_request_id_headers: tuple[str, ...] = ("x-request-id", "traceparent")
    slow_context: SlowContextFunc | None = None
    track_sizes: bool = True
    recent_minutes: int = 60
    recent_windows: tuple[int, ...] = (1, 5, 15)
    refresh_seconds: int | None = None
    prometheus: bool = False
    prometheus_prefix: str = "fastapi_stats"

    def __post_init__(self) -> None:
        if isinstance(self.basic_auth, tuple):
            object.__setattr__(self, "basic_auth", BasicAuth(*self.basic_auth))
        if not self.quantiles or any(not 0 < q <= 1 for q in self.quantiles):
            raise ValueError("quantiles must be a non-empty sequence of values in (0, 1]")
        object.__setattr__(self, "quantiles", tuple(sorted(set(self.quantiles))))
        for name in ("window_size", "max_routes", "max_slow_calls", "max_key_length"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.slow_threshold_ms is not None and self.slow_threshold_ms < 0:
            raise ValueError("slow_threshold_ms must be >= 0 or None")
        if not self.stats_path.startswith("/"):
            raise ValueError("stats_path must start with '/'")
        if self.unmatched not in _UNMATCHED_CHOICES:
            raise ValueError("unmatched must be one of 'group', 'raw', 'skip'")
        if any(not key for key in self.slow_thresholds):
            raise ValueError("slow_thresholds keys must be non-empty")
        if any(v is not None and v < 0 for v in self.slow_thresholds.values()):
            raise ValueError("slow_thresholds values must be >= 0 or None")
        object.__setattr__(self, "slow_thresholds", MappingProxyType(dict(self.slow_thresholds)))
        object.__setattr__(self, "redact_query_params", tuple(p.lower() for p in self.redact_query_params))
        object.__setattr__(self, "slow_request_id_headers", tuple(h.lower() for h in self.slow_request_id_headers))
        if not 0 <= self.recent_minutes <= 1440:
            raise ValueError("recent_minutes must be between 0 and 1440")
        if self.recent_minutes and any(not 1 <= w <= self.recent_minutes for w in self.recent_windows):
            raise ValueError("recent_windows values must be between 1 and recent_minutes")
        object.__setattr__(self, "recent_windows", tuple(sorted(set(self.recent_windows))))
        if self.refresh_seconds not in _REFRESH_CHOICES:
            raise ValueError("refresh_seconds must be None, 0, 5, 15 or 60")
        if not _PROMETHEUS_NAME.fullmatch(self.prometheus_prefix):
            raise ValueError("prometheus_prefix must match [a-zA-Z_:][a-zA-Z0-9_:]*")

    def slow_threshold_for(self, key: str) -> float | None:
        """Slow threshold in ms for ``key`` (``None`` = never slow).

        Lookup order, plain dict lookups only: the exact key (``"GET /a/{id}"``), then the route part without the
        method (``"/a/{id}"``, any method), then the global ``slow_threshold_ms``. An override of ``None``
        disables the slow log for that route.
        """
        if key in self.slow_thresholds:
            return self.slow_thresholds[key]
        route = key.partition(" ")[2]
        if route in self.slow_thresholds:
            return self.slow_thresholds[route]
        return self.slow_threshold_ms

    def slow_threshold_summary(self) -> str:
        """Heading text for the slow-call table, e.g. ``">= 500 ms, 2 route overrides"``."""
        text = "disabled" if self.slow_threshold_ms is None else f">= {self.slow_threshold_ms:g} ms"
        n = len(self.slow_thresholds)
        if n:
            text += f", {n} route override{'' if n == 1 else 's'}"
        return text

    @property
    def credentials(self) -> BasicAuth | None:
        """``basic_auth`` normalised to a ``BasicAuth`` (the tuple form is converted in ``__post_init__``)."""
        auth = self.basic_auth
        return BasicAuth(*auth) if isinstance(auth, tuple) else auth

    @property
    def excluded(self) -> tuple[str, ...]:
        base = self.stats_path.rstrip("/")
        return (*self.exclude_paths, base, base + ".json")


def quantile_label(q: float) -> str:
    """0.95 -> ``p95``, 0.999 -> ``p99.9``."""
    return f"p{q * 100:g}"
