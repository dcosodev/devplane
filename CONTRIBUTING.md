# Contributing to DevPlane

Thanks for helping improve DevPlane. It is alpha software, so focused issues and small, verifiable pull requests are especially valuable.

## Development setup

Prerequisites: Python 3.11+, [uv](https://docs.astral.sh/uv/), and Git.

```bash
git clone https://github.com/dcosodev/devplane.git
cd devplane
uv sync --extra test
uv run pytest
uv run devplane --help
```

Spec Kit and agent CLIs are not required for the unit suite. Tests use temporary repositories and fake subprocess runners and consume no model tokens.

## Workflow

1. Open an issue before substantial behavior or architecture changes.
2. Create a focused branch.
3. Add or update a failing test before changing behavior.
4. Keep changes small and preserve the boundaries in `docs/architecture.md`.
5. Run:

```bash
uv run pytest --cov=devplane --cov-report=term-missing --cov-fail-under=80
uv run python -m compileall -q src tests
uv run ruff check src tests
uv export --quiet --format requirements-txt --no-emit-project --extra test --output-file /tmp/devplane-audit-requirements.txt
uv run pip-audit --strict -r /tmp/devplane-audit-requirements.txt
uv run bandit -q -r src --severity-level medium
uv build
uv run twine check dist/*
uv run devplane --help
git diff --check
```

6. Update documentation and examples for user-visible changes.
7. Open a pull request using the repository template.

## Design constraints

- Catalog semantics must remain meaningful without Spec Kit or an agent runtime.
- GitHub Spec Kit remains an optional external workflow producer; do not vendor or fork it.
- Runtime adapters translate a neutral request and must not leak agent-specific schema into capabilities.
- A new adapter needs verified non-interactive argv, error, permission, and prompt tests.
- Source manifests are editable; `.devplane/generated/*` must remain deterministic.
- Catalog content must never execute during `sync`, `inspect`, `context`, or `validate`.
- Reject path and symlink escapes from project and catalog roots.
- Use argv arrays with `shell=False`; never construct agent shell strings.
- Do not bypass an agent runtime's permission controls.
- Keep agent work isolated from the project branch until explicit integration.
- Do not claim OS sandboxing, signed catalogs, or arbitrary agent compatibility.

## Reporting security issues

Do not open a public issue for a vulnerability. Follow `SECURITY.md`.

By contributing, you agree that your contribution is licensed under the MIT License.
