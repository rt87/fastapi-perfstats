# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Per-route call statistics for Starlette / FastAPI: call counts, errors and latency quantiles.
- Bounded slow-call log.
- Built-in HTML stats page, with all values escaped and no external assets.
- Runtime dependency on `starlette` only; `fastapi` is not required.
