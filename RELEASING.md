# Releasing

Publishing is **off**. Nothing is uploaded to PyPI (or anywhere else) until the owner explicitly decides on the
first release and enables it.

## What CI does today

`.github/workflows/release.yml` runs on every `v*` tag and on manual dispatch. The `build` job builds the sdist and
wheel with `uv build`, runs `twine check`, asserts that the wheel contains `fastapi_perfstats/py.typed` and no `tests/`,
and, on tag runs, that the tag (without the leading `v`) equals the version in `pyproject.toml`. The result is kept as
a workflow artifact only.

The `publish-pypi` job exists but is disabled with `if: false`. It uses PyPI trusted publishing (OIDC, no stored
credentials). There is no TestPyPI job and no GitHub release creation.

## How a release would be cut

1. Bump `version` in `pyproject.toml` (and note it in `CHANGELOG.md` if wanted).
2. Commit, then tag the commit `vX.Y.Z` (the version without `v` must match `pyproject.toml`) and push the tag.
3. Check that the `build` job is green.

## Enabling publishing (owner only)

1. On PyPI, add a trusted publisher for this repository: workflow `release.yml`, environment `pypi`.
2. In the repository settings, create the `pypi` environment and require the owner's approval for it.
3. Remove `if: false` (and its comment) from the `publish-pypi` job.
