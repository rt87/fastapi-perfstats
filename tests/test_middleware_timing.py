"""Middleware timing, failures after response start, disconnects and size tracking (#6)."""

import asyncio
from collections.abc import AsyncIterator, MutableMapping
from typing import Any

import pytest
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.types import Message

from fakes import RecordingStore
from fastapi_perfstats import StatsCollector, StatsConfig
from fastapi_perfstats.models import CLIENT_CLOSED_STATUS


def make_app(**kw: Any) -> tuple[FastAPI, StatsCollector, RecordingStore]:
    store = RecordingStore()
    collector = StatsCollector(StatsConfig(storage=store, **kw))
    app = FastAPI()
    app.add_middleware(collector.middleware)

    async def background() -> None:
        await asyncio.sleep(0.3)

    @app.get("/bg")
    async def bg(tasks: BackgroundTasks) -> dict[str, int]:
        tasks.add_task(background)
        return {"ok": 1}

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            yield b"a"
            await asyncio.sleep(0.2)
            yield b"b"

        return StreamingResponse(gen())

    @app.get("/broken")
    async def broken() -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            yield b"a"
            raise RuntimeError("boom")

        return StreamingResponse(gen())

    @app.post("/echo")
    async def echo(payload: dict[str, str]) -> dict[str, str]:
        return payload

    return app, collector, store


def drive(
    app: Any, messages: list[Message], scope_extra: MutableMapping[str, Any] | None = None
) -> tuple[list[Message], BaseException | None]:
    """Run the ASGI app by hand: ``messages`` are fed to ``receive`` one by one, then it blocks forever."""
    sent: list[Message] = []
    queue = list(messages)
    scope: dict[str, Any] = {"type": "http", "method": "GET", "path": "/x", **(scope_extra or {})}

    async def receive() -> Message:
        if queue:
            return queue.pop(0)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    async def run() -> BaseException | None:
        try:
            await app(scope, receive, send)
        except Exception as exc:
            return exc
        return None

    return sent, asyncio.run(run())


def test_background_task_is_not_counted():
    app, _, store = make_app()
    assert TestClient(app).get("/bg").status_code == 200
    assert store.records[0].duration_ms < 200


def test_streaming_pauses_are_counted():
    app, _, store = make_app()
    assert TestClient(app).get("/stream").text == "ab"
    assert store.records[0].duration_ms >= 200


def test_generator_failing_after_first_chunk_is_500():
    app, _, store = make_app()
    with pytest.raises(RuntimeError):
        TestClient(app).get("/broken")
    assert store.records[0].status == 500


def test_normal_response_keeps_its_status():
    app, _, store = make_app()
    assert TestClient(app).get("/nope").status_code == 404
    assert store.records[0].status == 404


def test_exception_before_response_start_is_500():
    _, collector, store = make_app()

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        raise ValueError

    _, exc = drive(collector.middleware(inner), [])
    assert isinstance(exc, ValueError)
    assert store.records[0].status == 500


def test_disconnect_before_completion_is_499():
    _, collector, store = make_app()

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x", "more_body": True})
        await receive()  # sees the disconnect, returns without a final body

    sent, exc = drive(collector.middleware(inner), [{"type": "http.disconnect"}])
    assert exc is None and len(sent) == 2
    assert store.records[0].status == CLIENT_CLOSED_STATUS == 499


def test_disconnect_after_completion_changes_nothing():
    _, collector, store = make_app()

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})
        await receive()

    drive(collector.middleware(inner), [{"type": "http.disconnect"}])
    assert store.records[0].status == 201


def test_duration_stops_at_final_chunk():
    _, collector, store = make_app()

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})
        await asyncio.sleep(0.3)  # work after the response, like a background task

    drive(collector.middleware(inner), [])
    assert store.records[0].duration_ms < 200


def test_sizes_are_tracked():
    app, _, store = make_app()
    TestClient(app).post("/echo", json={"a": "b"})
    rec = store.records[0]
    assert rec.request_bytes == len(b'{"a":"b"}') and rec.response_bytes == len(b'{"a":"b"}')


def test_sizes_off_passes_none():
    app, _, store = make_app(track_sizes=False)
    TestClient(app).post("/echo", json={"a": "b"})
    rec = store.records[0]
    assert rec.request_bytes is None and rec.response_bytes is None


def test_non_http_and_excluded_paths_pass_through():
    _, collector, store = make_app()
    calls: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        calls.append(scope["type"])

    mw = collector.middleware(inner)
    drive(mw, [], {"type": "websocket"})
    drive(mw, [], {"path": "/stats"})
    assert calls == ["websocket", "http"]
    assert store.records == []
