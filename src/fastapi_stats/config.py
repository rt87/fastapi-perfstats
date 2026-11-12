"""Single configuration object for fastapi-stats."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

DEFAULT_QUANTILES: tuple[float, ...] = (0.5, 0.95, 0.99)

KeyFunc = Callable[[Mapping[str, Any]], "str | None"]


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
