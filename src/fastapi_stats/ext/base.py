"""Extension protocol, column / page-part / endpoint descriptions and the no-op base extension."""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from starlette.requests import Request
from starlette.responses import Response

from fastapi_stats.config import StatsConfig
from fastapi_stats.models import CallRecord, RouteStats

if TYPE_CHECKING:
    from fastapi_stats.collector import StatsCollector


@dataclass(frozen=True)
class Column:
    table: Literal["freq", "rt", "slow", "sizes"]
    header: str
    order: int
    cell: Callable[[Any, StatsConfig], tuple[str, float | None]]  # (RouteStats or SlowCall, config) -> (text, sort)
    raw_html: bool = False  # True only for trusted markup (sparklines); text is escaped otherwise


@dataclass(frozen=True)
class PagePart:
    name: str
    html_top: Callable[[StatsCollector], str]  # trusted HTML after the intro paragraph; escapes its own data
    js: str = ""  # static script appended to the page script (CSP hash)
    body_attrs: Callable[[StatsConfig], dict[str, str]] = lambda config: {}  # data-* on <body>; values escaped


@dataclass(frozen=True)
class EndpointSpec:
    suffix: str  # e.g. "/reset", appended to stats_path
    methods: tuple[str, ...]
    enabled: Callable[[StatsConfig], bool]
    handler: Callable[[StatsCollector], Callable[[Request], Awaitable[Response]]]  # auth/headers added by router


class Extension(Protocol):
    """Hooks a feature can implement; ``call.key`` is always the final key (after ``other`` bucketing).

    SQLite hooks run inside the writer's transaction and must not commit.
    """

    name: str

    def sqlite_schema(self) -> tuple[str, ...]: ...  # idempotent CREATE TABLE/INDEX IF NOT EXISTS

    def memory_update(self, state: dict[str, Any], call: CallRecord) -> None: ...

    def memory_finish(self, row: RouteStats, state: dict[str, Any], config: StatsConfig, now: float) -> None: ...

    def sqlite_update(self, db: sqlite3.Connection, call: CallRecord) -> None: ...

    def sqlite_prune(self, db: sqlite3.Connection, config: StatsConfig, now: float) -> None: ...

    def sqlite_finish(
        self, rows: Mapping[str, RouteStats], db: sqlite3.Connection, config: StatsConfig, now: float
    ) -> None: ...

    def sqlite_reset(self, db: sqlite3.Connection) -> None: ...

    def columns(self, config: StatsConfig) -> tuple[Column, ...]: ...


class NoopExtension:
    """Implements ``Extension`` with no-ops; feature extensions subclass it or are created with their name."""

    def __init__(self, name: str = "noop") -> None:
        self.name = name

    def sqlite_schema(self) -> tuple[str, ...]:
        return ()

    def memory_update(self, state: dict[str, Any], call: CallRecord) -> None:
        return None

    def memory_finish(self, row: RouteStats, state: dict[str, Any], config: StatsConfig, now: float) -> None:
        return None

    def sqlite_update(self, db: sqlite3.Connection, call: CallRecord) -> None:
        return None

    def sqlite_prune(self, db: sqlite3.Connection, config: StatsConfig, now: float) -> None:
        return None

    def sqlite_finish(
        self, rows: Mapping[str, RouteStats], db: sqlite3.Connection, config: StatsConfig, now: float
    ) -> None:
        return None

    def sqlite_reset(self, db: sqlite3.Connection) -> None:
        return None

    def columns(self, config: StatsConfig) -> tuple[Column, ...]:
        return ()
