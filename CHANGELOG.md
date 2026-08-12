# Changelog

All notable changes to DevPlane are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.2] - 2026-08-12

### Security

- Reject symlinked generated-state roots and entries before transactional snapshots, preventing reads or cleanup outside `.devplane/generated`.

## [0.2.1] - 2026-08-12

### Fixed

- Restore both project configuration and all generated manifests/context bundles when `runtime` or `use-profile` regeneration fails.

## [0.2.0] - 2026-08-12

### Added

- Agent-neutral runtime contract with built-in Hermes, Claude Code, and OpenCode adapters.
- Configurable adapter, executable, provider, model, and scalar runtime options.
- Reusable catalog profiles composed from exact capability versions.
- Catalog-provided validation defaults for execution plans.
- Catalog-only project mode with no Spec Kit or agent dependency.
- `adapters`, `runtime`, and `use-profile` CLI commands.
- Public Python service, web frontend, documentation, and general-development example profiles.
- Dedicated catalog and runtime-adapter documentation.

### Changed

- Reframed DevPlane as an organizational capability catalog plus optional multi-agent control plane.
- Made `workflow` and `runtime` optional in project manifests.
- Normalized implementation prompts and audit metadata so they no longer assume MiniMax.
- Preserved legacy `runtime.agent: hermes` compatibility with historical MiniMax defaults.
- Made GitHub Spec Kit an optional external workflow producer rather than a catalog requirement.
- Preflight external tools according to the selected workflow.

### Security

- Runtime adapters build argv arrays and keep `shell=False`.
- Claude Code and OpenCode adapters do not bypass their permission systems.
- Documentation now states that worktrees are not OS sandboxes, declarative policy is enforced after execution, catalogs are unsigned prompt/configuration material, and local audit logs are not cryptographically chained.

## [0.1.0] - 2026-08-12

### Added

- Human-gated GitHub Spec Kit flow for specification, planning, and task artifacts.
- Deterministic capability catalog resolution and generated context validation.
- Repository profiling and explicit capability activation.
- Integrity-checked execution plans bound to Git, tasks, manifests, scopes, dependencies, and validations.
- Bounded MiniMax-M3 implementation sessions in isolated Git worktrees.
- Cumulative setup, foundational, parallel user-story, and polish execution.
- Post-execution commit, path-scope, governance, and native test verification.
- Persisted run status, selective retries, explicit fast-forward integration, and safe cleanup.
- Controlled end-to-end test suite, package build checks, dependency audit, static analysis, and GitHub Actions CI.
- Public documentation, security policy, contribution guide, community templates, MIT license, and trademark notice.

### Security

- Rejects path and symlink escapes, malformed Git identifiers, execution-plan drift, out-of-scope writes, governance changes, dirty worktrees, and changed integration bases.
- Uses safe YAML loading, argument-array subprocesses with `shell=False`, non-interactive child processes, and digest-only handling for private feature descriptions.

[Unreleased]: https://github.com/dcosodev/devplane/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/dcosodev/devplane/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/dcosodev/devplane/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/dcosodev/devplane/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dcosodev/devplane/releases/tag/v0.1.0
