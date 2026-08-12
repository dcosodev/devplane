# DevPlane project instructions

- Python 3.11+; use `uv` for environments and commands.
- Build features with test-first development.
- Keep GitHub Spec Kit as an external workflow engine; do not fork or vendor its implementation.
- Keep catalog and execution contracts independent from agent-specific CLIs.
- Built-in runtime adapters are Hermes, Claude Code, and OpenCode; preserve explicit adapter capability checks.
- Source manifests are editable; `.devplane/generated/*` is deterministic generated output.
- Never execute catalog-provided scripts during `sync`, `inspect`, `context`, or `validate`.
- Reject paths that escape the project or catalog root, including symlink escapes.
- Native validation: `uv run pytest && uv run devplane --help`.
