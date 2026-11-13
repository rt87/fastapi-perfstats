"""Overhead benchmark: microseconds per request with and without fastapi-stats (not part of the package).

Drives ASGI apps directly with an in-process fake ``receive``/``send`` (no HTTP client, no network), so it measures
the middleware / route class, not the client. Two workloads: ``trivial`` (a tiny response, the worst case for the
relative overhead) and ``realistic`` (builds and serialises a JSON payload of about 6 kB and does a fixed amount of
CPU work, roughly what a small real endpoint costs; no sleeping, so no timer noise). Numbers are indicative only.

Usage: ``uv run python benchmarks/overhead.py [--n 5000] [--repeats 5] [--json]``
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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

DEFAULT_N = 5_000
DEFAULT_REPEATS = 5
WARMUP = 2_000
PATH = "/items/1"
WORKLOADS = ("trivial", "realistic")
PAYLOAD_ITEMS = 60  # rows in the realistic JSON payload
HASH_ROUNDS = 100  # fixed CPU work of the realistic endpoint (chained SHA-256)

Scope = dict[str, Any]


@dataclass(frozen=True)
class Result:
    scenario: str
    us_per_request: float
    delta_us: float | None  # difference to the matching bare app; None for the bare apps themselves
    pct_of_request: float | None = None  # delta_us relative to the bare request time, in percent


@dataclass(frozen=True)
class Scenario:
    name: str
    baseline: str | None  # name of the matching bare scenario
    build: Callable[[Path], tuple[Any, StatsCollector | None]]  # temp dir -> (ASGI app, its collector)


def _payload(item_id: int, workload: str) -> dict[str, Any]:
    """The response body: tiny for ``trivial``; a payload plus fixed CPU work for ``realistic``."""
    if workload == "trivial":
        return {"id": item_id}
    digest = b"seed"
    for _ in range(HASH_ROUNDS):
        digest = hashlib.sha256(digest).digest()
    short = digest.hex()[:16]
    items = [
        {"id": i, "name": f"item-{i}", "price": i * 1.25, "tags": ["a", "b", "c"], "digest": short}
        for i in range(PAYLOAD_ITEMS)
    ]
    return {"id": item_id, "items": items}


def _starlette(collector: StatsCollector | None, workload: str = "trivial") -> Starlette:
    async def item(request: Any) -> JSONResponse:
        return JSONResponse(_payload(int(request.path_params["item_id"]), workload))

    app = Starlette(routes=[Route("/items/{item_id}", item)])
    if collector is not None:
        app.add_middleware(collector.middleware)
    return app


def _fastapi(collector: StatsCollector | None, workload: str = "trivial", use_route_class: bool = False) -> Any:
    from fastapi import APIRouter, FastAPI

    app = FastAPI()
    router = APIRouter(route_class=collector.route_class) if collector and use_route_class else APIRouter()

    @router.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, Any]:
        return _payload(item_id, workload)

    app.include_router(router)
    if collector is not None and not use_route_class:
        app.add_middleware(collector.middleware)
    return app


def _with(collector: StatsCollector, factory: Callable[..., Any], **kwargs: Any) -> tuple[Any, StatsCollector]:
    return factory(collector, **kwargs), collector


def _scenarios(workload: str) -> tuple[Scenario, ...]:
    def name(label: str) -> str:
        return f"{workload}: {label}"

    def starlette(collector: StatsCollector | None) -> Any:
        return _starlette(collector, workload)

    def fastapi(collector: StatsCollector | None, **kwargs: Any) -> Any:
        return _fastapi(collector, workload, **kwargs)

    def sqlite(tmp: Path) -> tuple[Any, StatsCollector]:
        config = StatsConfig(storage="sqlite", sqlite_path=str(tmp / "bench.db"))
        return _with(StatsCollector(config), starlette)

    return (
        Scenario(name("starlette bare"), None, lambda tmp: (starlette(None), None)),
        Scenario(
            name("starlette + middleware (memory)"),
            name("starlette bare"),
            lambda tmp: _with(StatsCollector(), starlette),
        ),
        Scenario(name("starlette + middleware (sqlite)"), name("starlette bare"), sqlite),
        Scenario(name("fastapi bare"), None, lambda tmp: (fastapi(None), None)),
        Scenario(name("fastapi + middleware"), name("fastapi bare"), lambda tmp: _with(StatsCollector(), fastapi)),
        Scenario(
            name("fastapi + route_class"),
            name("fastapi bare"),
            lambda tmp: _with(StatsCollector(), fastapi, use_route_class=True),
        ),
    )


SCENARIOS: tuple[Scenario, ...] = tuple(s for w in WORKLOADS for s in _scenarios(w))


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


def _result(scenario: Scenario, us: float, baseline_us: float | None) -> Result:
    if baseline_us is None:
        return Result(scenario.name, us, None, None)
    return Result(scenario.name, us, us - baseline_us, (us - baseline_us) / baseline_us * 100)


def run(n: int = DEFAULT_N, repeats: int = DEFAULT_REPEATS, names: Sequence[str] | None = None) -> list[Result]:
    """Run the scenarios; median microseconds per request, the delta to the bare app and that delta in percent."""
    scenarios = select(names)
    warmup = min(WARMUP, n)
    medians = {s.name: _measure(s, n, repeats, warmup) for s in scenarios}
    return [_result(s, medians[s.name], None if s.baseline is None else medians[s.baseline]) for s in scenarios]


def markdown(results: Sequence[Result]) -> str:
    rows = ["| Scenario | µs per request | added (µs) | added (% of bare request) |", "| --- | ---: | ---: | ---: |"]
    for r in results:
        added = "-" if r.delta_us is None else f"{r.delta_us:+.1f}"
        pct = "-" if r.pct_of_request is None else f"{r.pct_of_request:+.1f} %"
        rows.append(f"| {r.scenario} | {r.us_per_request:.1f} | {added} | {pct} |")
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
