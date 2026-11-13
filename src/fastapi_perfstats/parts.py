"""Registries of page parts and extra endpoints, in fixed order."""

from __future__ import annotations

from fastapi_perfstats import lifecycle, live, prometheus, reset
from fastapi_perfstats.ext.base import EndpointSpec, PagePart

PAGE_PARTS: tuple[PagePart, ...] = (lifecycle.PART, reset.PART, live.PART)
ENDPOINTS: tuple[EndpointSpec, ...] = (reset.ENDPOINT, prometheus.ENDPOINT)
