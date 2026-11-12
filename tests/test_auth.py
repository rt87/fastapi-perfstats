"""Pluggable access check (#9)."""

import logging

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from fastapi_stats import StatsCollector, StatsConfig, localhost_only, mount_stats

AUTH = ("admin", "change-me")


class Check:
    """Counts calls; sync or async."""

    def __init__(self, result: bool, is_async: bool = False) -> None:
        self.result = result
        self.calls = 0
        self.is_async = is_async

    def __call__(self, request: Request):  # noqa: ANN204
        self.calls += 1
        if self.is_async:

            async def coro() -> bool:
                return self.result

            return coro()
        return self.result


def client(config: StatsConfig) -> TestClient:
    app = Starlette()
    mount_stats(app, StatsCollector(config))
    return TestClient(app)


@pytest.mark.parametrize("is_async", [False, True])
def test_true_allows_html_and_json(is_async: bool) -> None:
    check = Check(True, is_async)
    cl = client(StatsConfig(access=check))
    assert cl.get("/stats").status_code == 200
    assert cl.get("/stats.json").status_code == 200
    assert check.calls == 2


@pytest.mark.parametrize("is_async", [False, True])
def test_false_gives_403_on_html_and_json(is_async: bool) -> None:
    cl = client(StatsConfig(access=Check(False, is_async)))
    for path in ("/stats", "/stats.json"):
        r = cl.get(path)
        assert r.status_code == 403
        assert r.text == "Forbidden"


def test_basic_auth_failure_does_not_call_the_check() -> None:
    check = Check(True)
    cl = client(StatsConfig(basic_auth=AUTH, access=check))
    assert cl.get("/stats").status_code == 401
    assert cl.get("/stats", auth=("admin", "wrong")).status_code == 401
    assert check.calls == 0


def test_correct_credentials_and_false_check_gives_403() -> None:
    check = Check(False)
    cl = client(StatsConfig(basic_auth=AUTH, access=check))
    assert cl.get("/stats", auth=AUTH).status_code == 403
    assert check.calls == 1


def test_correct_credentials_and_true_check_allows() -> None:
    assert client(StatsConfig(basic_auth=AUTH, access=Check(True))).get("/stats", auth=AUTH).status_code == 200


def test_exception_in_check_propagates() -> None:
    def boom(request: Request) -> bool:
        raise RuntimeError("check failed")

    app = Starlette()
    mount_stats(app, StatsCollector(StatsConfig(access=boom)))
    with pytest.raises(RuntimeError, match="check failed"):
        TestClient(app).get("/stats")


def test_no_access_and_no_basic_auth_is_open() -> None:
    assert client(StatsConfig()).get("/stats").status_code == 200


def request_from(client_addr: tuple[str, int] | None) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "client": client_addr})


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_localhost_only_true_for_loopback(host: str) -> None:
    assert localhost_only(request_from((host, 1))) is True


@pytest.mark.parametrize("addr", [("10.0.0.5", 1), ("192.168.1.2", 1), None])
def test_localhost_only_false_for_other_hosts_and_missing_client(addr: tuple[str, int] | None) -> None:
    assert localhost_only(request_from(addr)) is False


def test_localhost_only_works_as_access_check() -> None:
    # TestClient reports the client host "testclient", which is not loopback
    assert client(StatsConfig(access=localhost_only)).get("/stats").status_code == 403


def test_no_warning_when_only_access_is_set(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        client(StatsConfig(access=Check(True)))
    assert "WITHOUT authentication" not in caplog.text


def test_warning_when_nothing_protects_the_page(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        client(StatsConfig())
    assert "WITHOUT authentication" in caplog.text
