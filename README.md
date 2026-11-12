# fastapi-stats

[![CI](https://github.com/rt87/fastapi-stats/actions/workflows/ci.yml/badge.svg)](https://github.com/rt87/fastapi-stats/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12--3.15-blue)
![License](https://img.shields.io/github/license/rt87/fastapi-stats)
![Tests](https://img.shields.io/badge/tests-79%20passing-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)
![Ruff](https://img.shields.io/badge/lint-ruff-261230)
![mypy](https://img.shields.io/badge/checked%20with-mypy%20strict-2a6db2)
![Status](https://img.shields.io/badge/status-prototype-orange)

Tiny per-route call statistics for Starlette / FastAPI, in the spirit of `pg_stat_statements`: call counts,
errors, latency quantiles, a bounded slow-call log and a built-in HTML stats page.

## Contents

- [Features](#features) - [Install](#install) - [Quick start](#quick-start) - [The stats page](#the-stats-page)
- [Configuration](#configuration) - [pg_stat-like behaviour](#pg_stat-like-behaviour)
- [Limits and caveats](#limits-and-caveats) - [Demo](#demo)

## Features

- Pure ASGI middleware (no `BaseHTTPMiddleware`), or a route class / decorator for single routes.
- Key = `METHOD` + route template (`GET /items/{item_id}` is one key); raw path only for unmatched requests.
- Per key: count, 5xx and 4xx counts, min / mean / max and configurable quantiles (default p50, p95, p99).
- Storage: in-memory (default) or SQLite (stdlib), both size-bounded, behind one small ABC.
- Slow-call log with a configurable threshold.
- HTML page (sortable, filterable, dark/light, no external assets) plus JSON, optional HTTP Basic auth.
- Runtime dependency: `starlette` only; works on Starlette and FastAPI apps.

## Install

```bash
pip install fastapi-stats
# until it is released, straight from the repository:
pip install "git+https://github.com/rt87/fastapi-stats"
```

Python 3.12-3.15. The only dependency is `starlette`; `fastapi-stats` never depends on `fastapi` (so a future
`fastapi[fastapi-stats]` extra stays possible). Middleware, the stats page and `mount_stats` work on plain Starlette
apps; only `StatsRoute` / `collector.route_class` and `dependencies=[Depends(...)]` need FastAPI installed (clear
`ImportError` otherwise).

## Quick start

App level (all routes):

```python
from fastapi import FastAPI
from fastapi_stats import StatsCollector, StatsConfig, mount_stats

collector = StatsCollector(StatsConfig(basic_auth=("admin", "change-me")))
app = FastAPI()
app.add_middleware(collector.middleware)
mount_stats(app, collector)  # page at /stats, JSON at /stats.json
```

Route level (one router, or a single function):

```python
from fastapi import APIRouter

from fastapi_stats import StatsRoute

router = APIRouter(route_class=StatsRoute)  # records into the process-wide default collector
router = APIRouter(route_class=collector.route_class)  # or: bound to your own collector (and its config)


@app.get("/one")
@collector.track("one")  # a single async endpoint
async def one(): ...
```

`StatsRoute` uses a default collector (plain `StatsConfig()` unless you call `set_default_collector(my_collector)`
first); pass that same collector to `mount_stats` to see its data. Do not combine middleware and route class / decorator for the same routes (they would count twice).

## The stats page

Served at `stats_path` (default `/stats`), JSON at `/stats.json`. Three plain tables, each starting with separate `Method` and `Route` columns (the JSON has `method` and `route`
fields next to the combined `key`): call frequency per route
(count, 5xx, 4xx, share, calls per minute), longest runtimes per route (max, quantiles, mean, min) and the slow-call
list (time, route, duration, status). Click a header to sort, type in the filter box to narrow rows. Colours follow
`prefers-color-scheme`. The small script is inline, there are no CDN or framework requests, and every string
(route keys come from request data) is HTML-escaped.

**The page exposes your API structure and timings: do not leave it public.** Set `basic_auth=("user", "pw")`
(or `BasicAuth(...)`); it covers HTML and JSON, compares with `secrets.compare_digest` and answers 401 with
`WWW-Authenticate`. Mounting without auth logs a warning. `dependencies=[Depends(...)]` on `mount_stats` /
`stats_router` can be used in addition. Basic auth is only sensible over HTTPS or on localhost.

## Configuration

All options live in the frozen dataclass `StatsConfig`.

| Option | Default | Meaning |
| --- | --- | --- |
| `quantiles` | `(0.5, 0.95, 0.99)` | Replaces the defaults when given; values in (0, 1] |
| `storage` | `"memory"` | `"memory"`, `"sqlite"` or a custom `StatsStore` instance |
| `sqlite_path` | `"fastapi_stats.db"` | SQLite file |
| `window_size` | `1000` | Last N durations kept per route (quantile window) |
| `max_routes` | `200` | Distinct keys; further keys go to the `other` bucket |
| `slow_threshold_ms` | `500.0` | Calls at or above are logged individually; `None` disables |
| `max_slow_calls` | `200` | Slow-log size, oldest dropped first |
| `stats_path` | `"/stats"` | Page URL |
| `exclude_paths` | `("/health",)` | Never counted (the stats paths always are excluded) |
| `key_func` | `None` | `scope -> key` (or `None` to skip the call) |
| `max_key_length` | `200` | Longer keys are truncated |
| `basic_auth` | `None` | `(user, password)` or `BasicAuth` |

## pg_stat-like behaviour

- Counts, errors, min, max and mean are all-time aggregates per key; quantiles come from the last `window_size`
  durations, computed on read with the nearest-rank method.
- Only calls with duration >= `slow_threshold_ms` enter the slow log (timestamp, key, duration, status); it is a
  bounded list that keeps the most recent `max_slow_calls` entries.
- SQLite: `record()` only enqueues; one writer thread applies batches in a single transaction, so the event loop
  never waits for disk. If the queue (10 000) is full, records are dropped (`dropped` counter) rather than blocking.
  Rows are pruned to `window_size` per route and `max_slow_calls`.

## Limits and caveats

- Statistics are per process; there is no merging across workers. With SQLite, several processes write into the
  same file (WAL, atomic upserts, counts add up); in-memory shows only the own process.
- Prototype: API may change; not load-tested. SQLite stats can lose the last few records on a hard crash.
- Unmatched paths (404s) are keyed by raw path until `max_routes` is reached, then bucketed as `other`.
- Mounted sub-applications are keyed by the inner route template as set in `scope["route"]`, if any.

## Development

```bash
uv sync
uv run pytest      # tests + coverage
uv run ruff check .
uv run mypy        # strict; tests/demo only relax the "must be annotated" rules
```

The package ships `py.typed`.

## Demo

```bash
uv sync
uv run python examples/demo.py   # then open http://127.0.0.1:8000/items/1 and /stats (admin / change-me)
```
