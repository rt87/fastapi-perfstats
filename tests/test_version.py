"""``fastapi_perfstats.__version__`` (#20)."""

import importlib
import importlib.metadata
from collections.abc import Iterator

import pytest

import fastapi_perfstats


@pytest.fixture(autouse=True)
def restore_module() -> Iterator[None]:
    yield
    importlib.reload(fastapi_perfstats)


def test_version_matches_installed_metadata() -> None:
    assert fastapi_perfstats.__version__ == importlib.metadata.version("fastapi-perfstats")
    assert fastapi_perfstats.__version__ != "0+unknown"


def test_version_falls_back_when_package_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    reloaded = importlib.reload(fastapi_perfstats)
    assert reloaded.__version__ == "0+unknown"


def test_version_is_exported() -> None:
    assert "__version__" in fastapi_perfstats.__all__
