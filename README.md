# fastapi-stats

[![CI](https://github.com/rt87/fastapi-stats/actions/workflows/ci.yml/badge.svg)](https://github.com/rt87/fastapi-stats/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12--3.15-blue)
![License](https://img.shields.io/github/license/rt87/fastapi-stats)
![Ruff](https://img.shields.io/badge/lint-ruff-261230)
![mypy](https://img.shields.io/badge/checked%20with-mypy%20strict-2a6db2)
![Status](https://img.shields.io/badge/status-alpha-yellow)
![PyPI](https://img.shields.io/badge/PyPI-not%20yet%20published-lightgrey)

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
| `unmatched` | `"group"` | How unmatched requests are keyed: `group`, `raw` or `skip` |
| `close_on_shutdown` | `True` | Close the store on app shutdown |
| `access` | `None` | Extra access check `request -> bool` for the stats endpoints |
| `allow_reset` | `False` | Enable the reset endpoint |
| `slow_thresholds` | `{}` | Per-key slow thresholds in ms |
| `slow_capture_query` | `False` | Store the redacted query string with slow calls |
| `redact_query_params` | see source | Query parameter name fragments that are redacted |
| `slow_request_id_headers` | `("x-request-id", "traceparent")` | Headers tried for a request id |
| `slow_context` | `None` | Hook adding context to slow calls |
| `track_sizes` | `True` | Track request/response payload sizes |
| `recent_minutes` | `60` | Minutes of recent activity kept (0..1440, 0 = off) |
| `recent_windows` | `(1, 5, 15)` | Recent windows in minutes |
| `refresh_seconds` | `None` | Initial value of the page's auto-refresh selector (`None`/0 = Off, 5, 15, 60) |
| `prometheus` | `False` | Enable the Prometheus text endpoint |
| `prometheus_prefix` | `"fastapi_stats"` | Prometheus metric name prefix |

## pg_stat-like behaviour

- Counts, errors, min, max and mean are all-time aggregates per key; quantiles come from the last `window_size`
  durations, computed on read with the nearest-rank method.
- Only calls with duration >= `slow_threshold_ms` enter the slow log (timestamp, key, duration, status); it is a
  bounded list that keeps the most recent `max_slow_calls` entries.
- SQLite: `record()` only enqueues; one writer thread applies batches in a single transaction, so the event loop
  never waits for disk. If the queue (10 000) is full, records are dropped (`dropped` counter) rather than blocking.
  Rows are pruned to `window_size` per route and `max_slow_calls`.

## Feature details

### Storage

Both stores (`MemoryStore`, `SqliteStore`) take an optional `extensions` sequence (`None` = `fastapi_stats.ext.EXTENSIONS`)
and call each extension's hooks with the final key (also for the `other` bucket).

- **Slow calls** keep `query`, `request_id` and `context` (memory: on `SlowCall`; SQLite: table `slow_call_extra`, context
  as JSON, pruned together with the slow log). The threshold comes from `config.slow_threshold_for(key)`.
- **`reset()`** clears all statistics and every extension's state. The SQLite store applies it in order with the writer
  queue: records enqueued before the call are gone, later ones are kept. On a closed store it raises `RuntimeError`.
- **`dropped`** counts records the SQLite queue could not take; **`close()`** may be called more than once.
- If an extension hook or SQL statement fails inside the SQLite writer, that batch is rolled back and logged; the
  writer keeps running.

### SQLite schema

A new or empty database file gets the core tables (`routes`, `durations`, `slow_calls`) and is stamped with
`PRAGMA user_version = 1`. Opening a stamped file of the same version changes nothing. A file with another version, or
with tables but no stamp, is refused with a `RuntimeError` naming the file and both versions; nothing is migrated or
deleted, so delete the file or point `sqlite_path` elsewhere. Concurrent workers are serialised with `BEGIN IMMEDIATE`.
Extension tables are created idempotently by their owners and are not part of the version check.

### Mounted sub-applications

Routes under `Mount(...)` are keyed with the full mount prefix, e.g. `GET /v1/items/{item_id}` and
`GET /v2/items/{item_id}` stay separate; nested mounts give the whole prefix. A proxy root path (`--root-path`) never
appears in keys, and FastAPI `include_router(prefix=...)` is not prefixed twice. A mounted ASGI app without inner
routing (`app.mount("/static", ...)`) is keyed by the mount path (`GET /static`). The suite runs in CI against the
lowest allowed Starlette (0.37), so the declared floor is tested.

Limit: a mount path with parameters (`Mount("/tenants/{tenant}", ...)`) contributes the raw value (one key per
tenant), because the scope only carries the matched path, not the mount template. Avoid parameters in mount paths, or
set `key_func`.

### Unmatched requests

Requests that match no route (404s from scanners, `/.env`, ...) must not use up the `max_routes` slots, so
`StatsConfig.unmatched` decides how they are keyed:

- `"group"` (default): one key per method, `GET <unmatched>`, whatever the path.
- `"raw"`: the raw path becomes the key (unbounded until `max_routes` is reached, then `other`).
- `"skip"`: nothing is recorded.

HTTP methods outside the standard set are keyed as `OTHER` (`PROPFIND /items/1` becomes `OTHER /items/{item_id}`).
A method mismatch on an existing route (405) keeps its route template. A custom `key_func` bypasses all of this.

### Timing semantics

`StatsMiddleware` stops the clock when the last body chunk is handed to the server, so Starlette background tasks
that run afterwards are not counted, while a streaming response is measured including its pauses between chunks.
(`StatsRoute` and `track()` measure until the handler returns; body streaming is excluded there.)

- A client disconnect before the response completed is recorded as status `499` (counts as a 4xx error).
- An exception after the response started but before it completed is recorded as `500`.
- Disconnect messages arriving after completion change nothing.
- With `track_sizes`, request and response body bytes are summed from the ASGI messages.

### Lifecycle

`StatsMiddleware` handles the ASGI lifespan: when the app reports `lifespan.shutdown.complete` or
`lifespan.shutdown.failed`, the store is closed first (in a worker thread, as it may wait up to 10 s for the SQLite
writer), so queued records are persisted. Set `close_on_shutdown=False` to leave the store open. `close()` may be
called more than once.

If you only use `StatsRoute` or `track()` (no middleware), close the collector yourself:

```python
@asynccontextmanager
async def lifespan(app):
    yield
    collector.close()


app = FastAPI(lifespan=lifespan)
```

Records the SQLite writer queue could not take are counted: the JSON has `"dropped"` and the page shows
`N records dropped (writer queue full)` when it is above 0.

### Security headers

The HTML page is CSP-compatible without extra files: its inline `<style>` and `<script>` are allowed by SHA-256
hashes computed from the exact served text, so it also works under a strict global policy. It is sent with
`Content-Security-Policy: default-src 'none'; style-src <hash>; script-src <hash>; img-src 'self' data:; connect-src
'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` and `Cache-Control: no-store`. JSON and the 401/403 responses
carry `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. The page uses no inline event handlers, no
`style="..."` attributes and no dynamic content inside `<script>` / `<style>`; data is passed via `data-*` attributes.

### Access control

Every stats endpoint (page, JSON and the optional reset / metrics endpoints) runs the same check: HTTP Basic first
(`basic_auth`, 401 on failure), then `access`, a callable `request -> bool` that may be sync or async. `False` gives
`403 Forbidden`; an exception in the check propagates (500) and is never swallowed. The check is not called when Basic
auth already failed. No "mounted WITHOUT authentication" warning is logged when `access` is set.

```python
from fastapi_stats import StatsConfig, localhost_only


async def staff_only(request) -> bool:
    return await my_session_store.is_staff(request.cookies.get("session"))


StatsConfig(access=staff_only)  # any sync or async check
StatsConfig(access=localhost_only)  # loopback clients only (127.0.0.1, ::1)
```

`localhost_only` is unreliable behind a reverse proxy, where every client appears as the proxy's address.

### Reset

`collector.reset()` clears all statistics (routes, durations, slow log, extension state) without a restart; with SQLite
the reset is applied in order with the writer queue, so records recorded before the call are deleted and later ones
are kept. A custom store without `reset()` raises `NotImplementedError`.

With `allow_reset=True` (default `False`) the stats router also serves `POST {stats_path}/reset` and the page shows a
"Reset statistics" button (confirm dialog, then reload). The endpoint uses the same authorization as the page and
additionally needs the header `X-Stats-Reset: 1`: browsers send Basic credentials automatically, but an HTML form cannot
set a custom header and a cross-origin `fetch` with it needs a CORS preflight, which is never allowed.

| Result | Meaning |
| --- | --- |
| 204 (`Cache-Control: no-store`) | statistics cleared |
| 400 | header `X-Stats-Reset: 1` missing |
| 401 / 403 | authorization failed |
| 405 | not a POST (or `allow_reset` is off: no such route) |
| 501 | the store does not support reset |

The button posts to the page's own URL plus `/reset` (taken from `location.pathname` in the browser), so it is right
for any `path`, mount prefix or proxy root path. The reset path lies below `stats_path`, so it is never recorded.

```bash
curl -X POST -H "X-Stats-Reset: 1" -u admin:secret http://localhost:8000/stats/reset
```

### Error rates and stddev

Each route shows `5xx %` and `4xx %` (errors divided by all calls of that route) and `Stddev`, the population
standard deviation of its durations in ms (`sqrt(E[x^2] - mean^2)`, computed from a running sum of squares over all
calls, not just the quantile window). A route with a single call has a stddev of `0.0`. The JSON has
`error_rate_4xx`, `error_rate_5xx` and `stddev_ms` per route. With SQLite the sums live in the table `ext_stddev`;
memory and SQLite give identical numbers.

### Per-route slow thresholds

`slow_thresholds` overrides `slow_threshold_ms` per route, for example when a report export legitimately takes
seconds and a lookup should take milliseconds:

```python
StatsConfig(
    slow_threshold_ms=500,
    slow_thresholds={
        "GET /reports/{report_id}": 3000,  # full key: this method and route only
        "/lookup/{item_id}": 20,  # route template: any method
        "/internal/export": None,  # None: never logged as slow
    },
)
```

The lookup order is the exact key, then the route template without the method, then the global threshold. Only plain
dict lookups are used (no patterns or globs). A global `slow_threshold_ms=None` still logs routes that have their own
value. Values must be `None` or `>= 0`, keys must not be empty. The slow-call heading shows the number of overrides
(`Slow calls (>= 500 ms, 2 route overrides)`) and the JSON has `slow_thresholds`.

### Slow-call context

A slow-call entry can carry a request or trace id, the query string and custom context. The extras are computed only
for calls that are actually slow, so fast calls cost nothing extra.

- **Request id** (on by default): the first header from `slow_request_id_headers` (default `x-request-id`,
  `traceparent`) that is present. For `traceparent` only the 32-hex trace id is kept; invalid values are ignored. Values
  are cut to 128 characters.
- **Query string** (`slow_capture_query=True`, off by default): stored with the values of parameters whose NAME contains
  one of `redact_query_params` (case-insensitive substring, default `pass`, `tok`, `secret`, `key`, `auth`, `sig`,
  `session`, `code`) replaced by `[redacted]`; at most 500 characters.
- **Context hook** (`slow_context`): a function `scope -> {name: value}` for your own correlation data. At most 10
  items, keys and values cut to 200 characters. If it raises, the request is unaffected: the error is logged once per
  exception type on the `fastapi_stats` logger and `{"context_error": "<ExceptionTypeName>"}` is stored.

Privacy: the defaults store no query and no custom context. Request and response bodies and all headers other than the
configured id headers are never read or stored. The page and the JSON show `Request id`, `Query` and `Context`; the
page escapes all of them.

### Payload sizes

With `track_sizes=True` (default) the middleware sums the request body bytes and the response body bytes of every call
(streamed bodies across all chunks). The page gets a "Payload sizes" table (`Mean request`, `Mean response`,
`Max response`, in `B`/`KiB`/`MiB` with the raw byte count used for sorting) and the JSON route entries gain
`mean_request_bytes`, `mean_response_bytes` and `max_response_bytes`.

- Only calls with both sizes known contribute; `StatsRoute`, `track()` and `track_sizes=False` leave them unknown, shown
  as `n/a` (`None` in JSON).
- Sizes are body bytes before/after any compression by an outer layer, without headers. The body of a `HEAD` response
  is not counted (servers do not send it).
- SQLite keeps the aggregates in its own table `ext_sizes` (one row per route).

### Recent activity

Everything else is all-time; the recent columns show what happens now, like a load average. Calls are counted in
one-minute buckets (wall clock, `int(ts // 60)`); `recent_minutes` (default 60, max 1440, `0` turns the feature off)
buckets are kept per route, and `recent_windows` (default `(1, 5, 15)`) picks the windows. The frequency table adds
`Calls/min (Nm)` per window and `5xx % (Nm)` for the largest window; the plain `Calls/min` is then labelled
`Calls/min (all)`. The JSON has `recent` (`"1m"`, `"5m"`, ... with `calls`, `calls_per_min`, `errors_5xx`,
`error_rate_5xx`, `mean_ms`) and `series` (per-minute counts, oldest first, zero-filled, `recent_minutes` long) per
route. A window covers the current, partial minute plus the previous `N - 1`; for a route younger than the window the
rate is divided by its age. A timestamp that goes backwards (clock change) is counted in the newest bucket. SQLite keeps
the buckets in the table `ext_recent`, pruned on every write batch; with several worker processes the counts add up.
Recent quantiles are not offered (they would need histograms).

### Live page

The page has an auto-refresh selector (Off / 5 s / 15 s / 60 s); `refresh_seconds` (`0`, `5`, `15` or `60`) sets its
initial value, `None` (default) and `0` start it Off. The choice is remembered in the browser
(`localStorage`). A refresh fetches the page again and swaps the table bodies, so sort order and filter text are kept;
if the fetch fails a "refresh failed" note appears and the old data stays.

With `recent_minutes > 0` the frequency table gets a `Last N min` column: an inline SVG sparkline of calls per minute
(oldest to newest) per route, no external assets.

### Prometheus

Set `prometheus=True` to serve the statistics in the Prometheus text format at `{stats_path}/metrics` (protected like
the other stats endpoints, not recorded itself). Metric names start with `prometheus_prefix` (default `fastapi_stats`):
`_requests_total`, `_responses_errors_total{status_class}`, `_request_duration_seconds` (summary) and `_dropped_records_total`
(always exposed, 0 until the store drops something). All families keep their HELP/TYPE lines even before the first
request. Labels are `method` and `route`.

```yaml
scrape_configs:
  - job_name: my-api
    metrics_path: /stats/metrics
    basic_auth: {username: admin, password_file: /etc/prometheus/stats-password}
    static_configs:
      - targets: ["api.example.internal:8000"]
```

Quantiles cover the last `window_size` calls per route and cannot be aggregated across instances. For fleet-wide SLOs
use prometheus-fastapi-instrumentator or prometheus-client histograms.

### Overhead

`benchmarks/overhead.py` drives ASGI apps in-process (no HTTP client, no network) and reports the median microseconds
per request over 5 repeats of 5 000 requests (after 2 000 warm-up requests; a full run takes about 20 s):
`uv run python benchmarks/overhead.py [--n N] [--repeats R] [--json]`. One run on Python 3.14, Windows 11, a
laptop-class Intel CPU (measured with 20 000 requests per repeat; the default is now smaller to keep a run short):

| Scenario | us/request | delta vs bare |
| --- | ---: | ---: |
| starlette bare | 9.0 | - |
| starlette + middleware (memory) | 15.8 | +6.8 |
| starlette + middleware (sqlite) | 60.0 | +51.1 |
| fastapi bare | 188.5 | - |
| fastapi + middleware | 232.8 | +44.3 |
| fastapi + route_class | 266.1 | +77.6 |

The numbers are indicative only: they depend on the machine, the Python version and the load while measuring (the
FastAPI deltas in particular vary between runs). The `Benchmark` workflow can run the script on demand in CI.

## Limits and caveats

- Statistics are per process; there is no merging across workers. With SQLite, several processes write into the
  same file (WAL, atomic upserts, counts add up); in-memory shows only the own process.
- Prototype: API may change; not load-tested. SQLite stats can lose the last few records on a hard crash.
- Exclusion (`exclude_paths`, and the stats paths themselves) compares the raw request path. A stats router mounted
  below a `Mount` prefix is therefore only excluded if the prefixed path (e.g. `/api/admin/stats`) is added to
  `exclude_paths`; otherwise the stats requests themselves are counted.
- Unmatched paths (404s) share one `<unmatched>` key per method by default (`unmatched`); `max_routes` still caps the
  number of distinct keys, the rest go to `other`.

## Development

```bash
uv sync
uv run pytest      # tests + coverage
uv run ruff check .
uv run ruff format --check .
uv run mypy        # strict; tests/demo only relax the "must be annotated" rules
```

The package ships `py.typed`.

## Demo

```bash
uv sync
uv run python examples/demo.py   # then open http://127.0.0.1:8000/items/1 and /stats (admin / change-me)
```
