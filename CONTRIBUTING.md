# Contributing to DevPlane

Thanks for helping improve DevPlane. The project is currently alpha software, so focused issues and small, verifiable pull requests are especially valuable.

## Development setup

Prerequisites:

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Git

```bash
git clone https://github.com/dcosodev/devplane.git
cd devplane
uv sync --extra test
uv run pytest
uv run devplane --help
```

GitHub Spec Kit and Hermes Agent are only required for real workflow integration tests; the unit suite uses isolated fixtures and does not consume model tokens.

## Workflow

1. Open an issue before substantial behavior or architecture changes.
2. Create a focused branch.
3. Add or update a failing test before changing behavior.
4. Keep changes small and preserve the security boundaries in `docs/architecture.md`.
5. Run the native checks:

```bash
uv run pytest
uv run python -m compileall -q src tests
uv run ruff check src tests
uv export --quiet --format requirements-txt --no-emit-project --extra test --output-file /tmp/devplane-audit-requirements.txt
uv run pip-audit --strict -r /tmp/devplane-audit-requirements.txt
uv run bandit -q -r src --severity-level medium
uv build
uv run devplane --help
git diff --check
```

6. Update documentation for user-visible changes.
7. Open a pull request using the repository template.

## Design constraints

- GitHub Spec Kit remains an external dependency; do not vendor or fork it here.
- Hermes Agent is the supported coordinator runtime for the MVP.
- Source manifests are editable; `.devplane/generated/*` must remain deterministic.
- Catalog content must never execute during `sync`, `inspect`, `context`, or `validate`.
- Reject path and symlink escapes from project and catalog roots.
- Keep model work isolated from the main project branch until explicit integration.

## Reporting security issues

Do not open a public issue for a vulnerability. Follow `SECURITY.md`.

By contributing, you agree that your contribution is licensed under the MIT License.
