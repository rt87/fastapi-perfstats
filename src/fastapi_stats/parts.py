"""Registries of page parts and extra endpoints, in fixed order."""

from __future__ import annotations

from fastapi_stats import lifecycle, live, prometheus, reset
from fastapi_stats.ext.base import EndpointSpec, PagePart

PAGE_PARTS: tuple[PagePart, ...] = (lifecycle.PART, reset.PART, live.PART)
ENDPOINTS: tuple[EndpointSpec, ...] = (reset.ENDPOINT, prometheus.ENDPOINT)
