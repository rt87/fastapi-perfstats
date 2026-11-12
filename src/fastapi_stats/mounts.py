"""Mounted sub-application support (owned by #4)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def mount_prefix(scope: Mapping[str, Any]) -> str:
    """Path prefix of the mounts a request went through. Stub: no prefix."""
    return ""
