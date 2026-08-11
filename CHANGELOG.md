# Changelog

All notable changes to DevPlane are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/dcosodev/devplane/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dcosodev/devplane/releases/tag/v0.1.0
