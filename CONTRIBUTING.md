# Contributing

Thanks for your interest in fastapi-perfstats.

## Setup

```bash
uv sync --all-groups
```

## Checks

All of these must pass before a change is merged:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

New code needs at least 90 % test coverage, with positive and negative tests.

## Design principles

- The only runtime dependency is `starlette`. Never depend on `fastapi`; it is a dev dependency only.
- Otherwise use the standard library only.
- Memory use is bounded: every collection that grows with traffic has a cap.
- Every string rendered on the stats page is escaped.
- The stats page loads no external assets.

## Releasing

See [RELEASING.md](RELEASING.md).

## Conduct

Be respectful.
