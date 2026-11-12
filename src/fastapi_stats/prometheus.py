"""Prometheus text export (owned by #17)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import Response

from fastapi_stats.config import quantile_label
from fastapi_stats.ext.base import EndpointSpec

if TYPE_CHECKING:
    from fastapi_stats.collector import StatsCollector


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _family(name: str, kind: str, help_text: str, samples: list[str]) -> list[str]:
    """HELP/TYPE header plus samples; a family without samples keeps its header lines."""
    return [f"# HELP {name} {_escape_help(help_text)}", f"# TYPE {name} {kind}", *samples]


def _number(value: float) -> str:
    return repr(float(value)) if not float(value).is_integer() else str(int(value))


def render_prometheus(collector: StatsCollector) -> str:
    """Prometheus text exposition (format 0.0.4) of the collector's per-route statistics.

    Quantiles come from the sliding window (last ``window_size`` calls per route) and are not aggregatable across
    instances. Every family is always exposed; the dropped-records counter is 0 until the store drops something.
    """
    from fastapi_stats.page import split_key  # lazy: page imports parts, which imports this module

    config = collector.config
    prefix = config.prometheus_prefix
    labels: dict[str, str] = {}
    routes = sorted(collector.store.routes(), key=lambda r: r.key)
    for r in routes:
        method, route = split_key(r.key)
        labels[r.key] = f'method="{_escape_label(method)}",route="{_escape_label(route)}"'
    requests = [f"{prefix}_requests_total{{{labels[r.key]}}} {r.count}" for r in routes]
    errors: list[str] = []
    summary: list[str] = []
    for r in routes:
        errors.append(f'{prefix}_responses_errors_total{{{labels[r.key]},status_class="4xx"}} {r.errors_4xx}')
        errors.append(f'{prefix}_responses_errors_total{{{labels[r.key]},status_class="5xx"}} {r.errors_5xx}')
        for q in config.quantiles:
            ms = r.quantiles.get(quantile_label(q))
            if ms is not None:
                summary.append(
                    f'{prefix}_request_duration_seconds{{{labels[r.key]},quantile="{q:g}"}} {_number(ms / 1000)}'
                )
        summary.append(
            f"{prefix}_request_duration_seconds_sum{{{labels[r.key]}}} {_number(r.mean_ms * r.count / 1000)}"
        )
        summary.append(f"{prefix}_request_duration_seconds_count{{{labels[r.key]}}} {r.count}")
    dropped = collector.store.dropped
    lines = [
        *_family(f"{prefix}_requests_total", "counter", "Requests handled per route.", requests),
        *_family(f"{prefix}_responses_errors_total", "counter", "Error responses per route and class.", errors),
        *_family(
            f"{prefix}_request_duration_seconds",
            "summary",
            "Request duration in seconds; quantiles over the last window_size calls per route.",
            summary,
        ),
        *_family(
            f"{prefix}_dropped_records_total",
            "counter",
            "Records dropped because the store could not keep up.",
            [f"{prefix}_dropped_records_total {dropped}"],
        ),
    ]
    return "\n".join(lines) + "\n" if lines else ""


def _handler(collector: StatsCollector) -> Callable[[Request], Awaitable[Response]]:
    async def metrics(request: Request) -> Response:
        return Response(render_prometheus(collector), media_type=CONTENT_TYPE)

    return metrics


ENDPOINT = EndpointSpec(suffix="/metrics", methods=("GET",), enabled=lambda config: config.prometheus, handler=_handler)
