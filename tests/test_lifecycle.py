"""Lifecycle: close the store on shutdown (#7) and the dropped-records notice."""

from __future__ import annotations

import queue
from pathlib import Path
from typing import Any

import anyio
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from fakes import FIXED_TS, RecordingStore, make_call
from fastapi_stats import lifecycle
from fastapi_stats.collector import StatsCollector
from fastapi_stats.config import StatsConfig
from fastapi_stats.page import render_html, render_json
from fastapi_stats.stores import SqliteStore


def _app(collector: StatsCollector) -> FastAPI:
    app = FastAPI()
    app.add_middleware(collector.middleware)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def _keys(path: Path) -> list[str]:
    store = SqliteStore(StatsConfig(storage="sqlite", sqlite_path=str(path)), [])
    try:
        store.flush()
        return [r.key for r in store.routes()]
    finally:
        store.close()


class _ClosingProbe(RecordingStore):
    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _Dropping(RecordingStore):
    def __init__(self, dropped: int) -> None:
        super().__init__()
        self._n = dropped

    @property
    def dropped(self) -> int:
        return self._n


def _run_lifespan(collector: StatsCollector, app: Any, shutdown: str = "lifespan.shutdown.complete") -> list[Message]:
    sent: list[Message] = []
    inbox: list[Message] = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive() -> Message:
        return inbox.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    async def lifespan_app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await send({"type": shutdown})

    async def main() -> None:
        await lifecycle.handle_lifespan(collector, app or lifespan_app, {"type": "lifespan"}, receive, send)

    anyio.run(main)
    return sent


# ---- closing on shutdown ---------------------------------------------------------------------------------
def test_shutdown_persists_all_records(tmp_path: Path) -> None:
    path = tmp_path / "stats.db"
    collector = StatsCollector(StatsConfig(storage="sqlite", sqlite_path=str(path)))
    with TestClient(_app(collector)) as client:
        assert client.get("/ping").status_code == 200
    assert _keys(path) == ["GET /ping"]


def test_shutdown_closes_store_before_forwarding_message() -> None:
    collector = StatsCollector()
    probe = _ClosingProbe()
    collector.store = probe
    seen_closed: list[int] = []

    async def send_probe(message: Message) -> None:
        seen_closed.append(probe.closed)

    async def lifespan_app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "lifespan.shutdown.complete"})

    async def main() -> None:
        await lifecycle.handle_lifespan(collector, lifespan_app, {"type": "lifespan"}, _never, send_probe)

    anyio.run(main)
    assert seen_closed == [1]


async def _never() -> Message:
    raise AssertionError("receive must not be called")


def test_shutdown_failed_also_closes() -> None:
    collector = StatsCollector()
    probe = _ClosingProbe()
    collector.store = probe
    sent = _run_lifespan(collector, None, shutdown="lifespan.shutdown.failed")
    assert probe.closed == 1
    assert [m["type"] for m in sent] == ["lifespan.startup.complete", "lifespan.shutdown.failed"]


def test_startup_messages_do_not_close() -> None:
    collector = StatsCollector()
    probe = _ClosingProbe()
    collector.store = probe

    async def startup_only(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "lifespan.startup.complete"})
        await send({"type": "lifespan.startup.failed", "message": "boom"})

    async def main() -> None:
        async def send(message: Message) -> None:
            pass

        await lifecycle.handle_lifespan(collector, startup_only, {"type": "lifespan"}, _never, send)

    anyio.run(main)
    assert probe.closed == 0


def test_close_on_shutdown_false_leaves_store_open(tmp_path: Path) -> None:
    path = tmp_path / "stats.db"
    collector = StatsCollector(StatsConfig(storage="sqlite", sqlite_path=str(path), close_on_shutdown=False))
    probe = _ClosingProbe()
    real = collector.store
    collector.store = probe
    try:
        with TestClient(_app(collector)):
            pass
        assert probe.closed == 0
    finally:
        real.close()


def test_close_on_shutdown_false_passes_messages_through() -> None:
    collector = StatsCollector(StatsConfig(close_on_shutdown=False))
    sent = _run_lifespan(collector, None)
    assert [m["type"] for m in sent] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


def test_app_without_lifespan_support_still_starts() -> None:
    collector = StatsCollector()

    async def plain(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            return  # an app that ignores lifespan
        raise AssertionError("only lifespan expected")

    assert _run_lifespan(collector, plain) == []


def test_closing_twice_is_harmless(tmp_path: Path) -> None:
    collector = StatsCollector(StatsConfig(storage="sqlite", sqlite_path=str(tmp_path / "s.db")))
    with TestClient(_app(collector)):
        pass
    collector.close()  # already closed by the shutdown


# ---- dropped records -------------------------------------------------------------------------------------
def test_json_always_has_dropped() -> None:
    collector = StatsCollector()
    assert render_json(collector)["dropped"] == 0
    collector.store = _Dropping(4)
    assert render_json(collector)["dropped"] == 4


def test_html_hides_notice_when_nothing_dropped() -> None:
    collector = StatsCollector()
    collector.record("GET /a", 5.0, 200)
    assert "records dropped" not in render_html(collector)
    assert lifecycle.PART.html_top(collector) == ""


def test_html_shows_notice_when_dropped() -> None:
    collector = StatsCollector()
    collector.store = _Dropping(7)
    assert '<p class="bad">7 records dropped (writer queue full)</p>' in render_html(collector)


def test_sqlite_queue_full_shows_in_page(tmp_path: Path) -> None:
    collector = StatsCollector(StatsConfig(storage="sqlite", sqlite_path=str(tmp_path / "q.db")))
    store = collector.store
    try:
        assert isinstance(store, SqliteStore)
        store.flush()
        full: queue.Queue[object] = queue.Queue(1)
        full.put("filler")
        real_q, store._q = store._q, full
        collector.record_call(make_call(ts=FIXED_TS))
        collector.record_call(make_call(ts=FIXED_TS))
        assert lifecycle.PART.html_top(collector) == '<p class="bad">2 records dropped (writer queue full)</p>'
        store._q = real_q  # rendering flushes the real queue, which would block on the full stand-in
        assert render_json(collector)["dropped"] == 2
        assert "2 records dropped" in render_html(collector)
    finally:
        collector.close()


def test_memory_store_dropped_is_zero() -> None:
    assert StatsCollector().store.dropped == 0
