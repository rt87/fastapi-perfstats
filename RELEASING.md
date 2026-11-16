# Releasing

## What CI does

`.github/workflows/release.yml` runs on every `v*` tag and on manual dispatch. The `build` job builds the sdist and
wheel with `uv build`, runs `twine check`, asserts that the wheel contains `fastapi_perfstats/py.typed` and no `tests/`,
and, on tag runs, that the tag (without the leading `v`) equals the version in `pyproject.toml`.

On tag runs only, the `publish-pypi` job uploads the build to PyPI through trusted publishing (OIDC, no stored
credentials). It runs in the `pypi` environment, which only accepts `v*` tags and waits for the owner's approval.
There is no TestPyPI job and no automatic GitHub release.

## Cutting a release

1. Bump `version` in `pyproject.toml` and add a section to `CHANGELOG.md`.
2. Commit and push to `main`; wait for CI to be green.
3. Tag the commit `vX.Y.Z` (the version without `v` must match `pyproject.toml`) and push the tag.
4. In the Actions tab, open the `Release` run and approve the `pypi` deployment.
