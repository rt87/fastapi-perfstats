# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.2] - 2026-11-17

### Changed

- License metadata as an SPDX expression (`MIT`) instead of the full license text.

## [0.1.1] - 2026-11-16

### Changed

- PyPI classifiers: `Topic :: System :: Monitoring` and `Topic :: Internet :: WWW/HTTP`.

## [0.1.0] - 2026-11-16

First alpha release.

### Added

- Per-route call statistics for Starlette / FastAPI: call counts, errors and latency quantiles.
- Bounded slow-call log.
- Built-in HTML stats page, with all values escaped and no external assets.
- Runtime dependency on `starlette` only; `fastapi` is not required.
