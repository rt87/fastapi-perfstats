"""Overhead benchmark: microseconds per request with and without fastapi-stats (not part of the package).

Drives ASGI apps directly with an in-process fake ``receive``/``send`` (no HTTP client, no network), so it measures
the middleware / route class, not the client. Numbers are indicative only.

Usage: ``uv run python benchmarks/overhead.py [--n 20000] [--repeats 5] [--json]``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from fastapi_stats import StatsCollector, StatsConfig

DEFAULT_N = 20_000
DEFAULT_REPEATS = 5
WARMUP = 2_000
PATH = "/items/1"

Scope = dict[str, Any]


@dataclass(frozen=True)
class Result:
    scenario: str
    us_per_request: float
    delta_us: float | None  # difference to the matching bare app; None for the bare apps themselves


@dataclass(frozen=True)
class Scenario:
    name: str
    baseline: str | None  # name of the matching bare scenario
    build: Callable[[Path], tuple[Any, StatsCollector | None]]  # temp dir -> (ASGI app, its collector)


def _starlette(collector: StatsCollector | None) -> Starlette:
    async def item(request: Any) -> JSONResponse:
        return JSONResponse({"id": int(request.path_params["item_id"])})

    app = Starlette(routes=[Route("/items/{item_id}", item)])
    if collector is not None:
        app.add_middleware(collector.middleware)
    return app


def _fastapi(collector: StatsCollector | None, use_route_class: bool = False) -> Any:
    from fastapi import APIRouter, FastAPI

    app = FastAPI()
    router = APIRouter(route_class=collector.route_class) if collector and use_route_class else APIRouter()

    @router.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    app.include_router(router)
    if collector is not None and not use_route_class:
        app.add_middleware(collector.middleware)
    return app


def _with(collector: StatsCollector, factory: Callable[..., Any], **kwargs: Any) -> tuple[Any, StatsCollector]:
    return factory(collector, **kwargs), collector


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("starlette bare", None, lambda tmp: (_starlette(None), None)),
    Scenario("starlette + middleware (memory)", "starlette bare", lambda tmp: _with(StatsCollector(), _starlette)),
    Scenario(
        "starlette + middleware (sqlite)",
        "starlette bare",
        lambda tmp: _with(StatsCollector(StatsConfig(storage="sqlite", sqlite_path=str(tmp / "bench.db"))), _starlette),
    ),
    Scenario("fastapi bare", None, lambda tmp: (_fastapi(None), None)),
    Scenario("fastapi + middleware", "fastapi bare", lambda tmp: _with(StatsCollector(), _fastapi)),
    Scenario(
        "fastapi + route_class", "fastapi bare", lambda tmp: _with(StatsCollector(), _fastapi, use_route_class=True)
    ),
)


def select(names: Sequence[str] | None = None) -> list[Scenario]:
    """The scenarios called ``names`` (all when ``None``), baselines included; unknown names raise ``ValueError``."""
    if names is None:
        return list(SCENARIOS)
    by_name = {s.name: s for s in SCENARIOS}
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise ValueError(f"unknown scenario(s) {unknown}; choose from {sorted(by_name)}")
    wanted = set(names)
    wanted |= {by_name[n].baseline for n in names if by_name[n].baseline}
    return [s for s in SCENARIOS if s.name in wanted]


def _scope() -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": PATH,
        "raw_path": PATH.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"bench")],
        "server": ("bench", 80),
        "client": ("127.0.0.1", 1234),
    }


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _send(message: dict[str, Any]) -> None:
    return None


async def _drive(app: Any, n: int) -> float:
    """Seconds taken for ``n`` sequential requests."""
    start = time.perf_counter()
    for _ in range(n):
        await app(_scope(), _receive, _send)
    return time.perf_counter() - start


def _measure(scenario: Scenario, n: int, repeats: int, warmup: int) -> float:
    with tempfile.TemporaryDirectory() as tmp:

        async def go() -> float:
            app, collector = scenario.build(Path(tmp))
            try:
                await _drive(app, warmup)
                runs = [await _drive(app, n) for _ in range(repeats)]
            finally:
                if collector is not None:
                    collector.store.close()  # releases the SQLite file before the temp dir is removed
            return statistics.median(runs) / n * 1e6

        return asyncio.run(go())


def run(n: int = DEFAULT_N, repeats: int = DEFAULT_REPEATS, names: Sequence[str] | None = None) -> list[Result]:
    """Run the scenarios and return the median microseconds per request plus the delta to the bare app."""
    scenarios = select(names)
    warmup = min(WARMUP, n)
    medians = {s.name: _measure(s, n, repeats, warmup) for s in scenarios}
    return [
        Result(s.name, medians[s.name], None if s.baseline is None else medians[s.name] - medians[s.baseline])
        for s in scenarios
    ]


def markdown(results: Sequence[Result]) -> str:
    rows = ["| Scenario | us/request | delta vs bare |", "| --- | ---: | ---: |"]
    for r in results:
        delta = "-" if r.delta_us is None else f"+{r.delta_us:.1f}"
        rows.append(f"| {r.scenario} | {r.us_per_request:.1f} | {delta} |")
    return "\n".join(rows)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=DEFAULT_N, help="requests per repeat")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS, help="repeats per scenario (median reported)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a Markdown table")
    args = parser.parse_args(argv)
    results = run(args.n, args.repeats)
    print(json.dumps([asdict(r) for r in results], indent=2) if args.json else markdown(results))


if __name__ == "__main__":
    main()
