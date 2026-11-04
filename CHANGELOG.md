# Changelog

All notable changes to DevPlane are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-11-04

### Added

- `devplane backlog specify <id>`: starts the governed Spec Kit flow for a story. Its title and body become the feature description, the `specify` phase runs through the same code as `devplane run --stop-after tasks`, and the story is linked to the feature run in a commit of its own that contains only the story file. If the phase fails nothing is linked. Integrating the feature later moves its linked `ready` and `in_progress` stories to `review`.
- `devplane backlog show` and `devplane backlog board` report the status of the feature run a story is linked to.
- `devplane backlog create`: writes a validated story file with a generated id (`US-NNN`), a slug from the title and exclusive creation, so an existing file is never overwritten. It checks the new story against the whole backlog (unique id, known dependencies) and records `backlog.story.created` in the audit log.
- `devplane backlog board`: the backlog as columns by status, with the planner's verdict on each ready story (runs next, queued in a later wave and why, deferred behind in-progress work, or waiting for dependencies) and the next batch. Output as text, Markdown tables or JSON.

## [0.4.0] - 2026-11-03

### Added

- Bounded write-scope grammar (`devplane.scopes`): literal segments, `*` and `?` within a segment, and `**` only as the final segment, with exact membership and overlap decisions.
- `devplane guard --policy <file>`: a Claude Code `PreToolUse` hook that blocks `Edit`, `Write`, `MultiEdit` and `NotebookEdit` calls outside an assignment's write scopes, outside its worktree (including through symlinks), or into governed roots. Every failure path exits with 2, the only code Claude Code treats as a block.
- Claude Code runtime option `writeGuard: true`: registers `devplane guard` as a per-assignment `PreToolUse` hook through `--settings`. The execution pipeline writes a per-assignment policy outside the worktree and fails the assignment, rather than running unguarded, if it cannot. Off by default; other adapters reject the option.
- Hermes write guard: `devplane guard --hermes` speaks Hermes' `pre_tool_call` shell-hook protocol for `write_file` and `patch` (including multi-file patches). The `writeGuard` runtime option now also works for Hermes: DevPlane exports the per-assignment policy as `DEVPLANE_GUARD_POLICY` and, before launching any agent, checks with `hermes hooks list` that the hook is installed, approved and covers both tools. Hermes keeps its hooks in its own global configuration, so you install the hook once and DevPlane never edits it.
- `devplane adapters` reports `write_guard` (`pre-execution` or `post-run-only`) for every adapter.
- `python -m devplane` entry point.
- `devplane backlog` (`check`, `list`, `show`, `next`, `plan`, `status`): a repository-scoped story backlog stored as Markdown files in `backlog/`, with dependency and cycle validation, a fixed status transition table and a planner that groups ready stories into parallel batches with pairwise disjoint write scopes. See `docs/backlog.md`.
- Stories can declare their own `validation` commands. They replace the run's default validation for that story only, are checked with the same no-shell rules the pipeline uses, and count as story content for drift detection. A run is refused before any claim if some story has no validation at all.
- `devplane backlog run`: claims the next batch of ready stories in a `chore(backlog): start …` commit that becomes the approved base, then implements it with parallel agents through the existing worktree pipeline (story id = assignment id, `write_scope` = allowed paths). `implement --execution-plan`, `retry` and `integrate` understand backlog plans; `integrate` records integrated stories as `review`. `validate_execution_plan` is now a reusable function.
- `devplane backlog run --batches N --integrate`: runs up to N waves in sequence, integrating each completed wave before planning the next, and stops at the first failed wave or integration, when no story is ready, or at the limit. Chaining requires `--integrate` because each wave builds on the integrated result of the previous one. `devplane integrate` and `backlog run` now share the same integration code.
- Audit events for the backlog: `backlog.status.changed` when `devplane backlog status` moves a story and `backlog.review.recorded` when `integrate` moves integrated stories to `review`, alongside the existing `backlog.run.started`.

### Changed

- **Behavior change:** Assignment prompts now ask agents not to add `Co-Authored-By` or other tool attribution lines to their commit messages. The Claude Code adapter also turns off Claude Code's own commit and pull-request attribution for DevPlane sessions (`--settings`), because those commits are fast-forwarded into the project. Set the Claude runtime option `commitAttribution: true` to keep it.
- Parallel-batch overlap detection now uses exact scope overlap instead of a prefix approximation. Disjoint patterns such as `src/*.py` and `src/*.txt` are no longer reported as overlapping.
- Execution plans express a trailing `dir/*` write scope as `dir/**`, preserving its reach over nested files.
- Errors that reject a dirty project or worktree (`claim`, `plan-execution`, `implement`, `integrate`, `cleanup`, worktree creation) now name the first files that block it, for example `project must be clean before integration: ?? calc/__pycache__/`.

### Fixed

- Untracked files that validation commands leave in a DevPlane worktree, such as `__pycache__/`, no longer make a run report `completed` and then fail `integrate` with "pipeline final worktree must be clean". DevPlane removes untracked, non-ignored files from the assignment, integration and gate worktrees after validation. Tracked files are never touched, so a validation that edits tracked files is still refused, and the cleanup refuses any directory that is not a worktree root.
- `devplane retry` now exits with status 1 when the retried run is not `completed`, like `implement`; it used to exit 0 for a failed run.

### Security

- **Behavior change:** Post-run allowed-path checks no longer let `*` cross directory boundaries. A scope such as `src/*.py` previously accepted `src/nested/a.py` through `fnmatch`; it now rejects it. Saved plans that relied on that behavior fail closed.

## [0.3.0] - 2026-11-02

### Added

- Spec Kit phases can run through Claude Code, Codex, or OpenCode as well as Hermes. `devplane init` and `devplane new` take `--integration`, which defaults to the runtime.
- Codex runtime adapter (`codex exec`).

### Fixed

- The Claude Code adapter now works. `--allowedTools` consumed the prompt, so every implementation session exited with an error.
- Execution plans no longer fail when a task names one of its feature's approved documents, such as `specs/001-demo/quickstart.md`. The reference grants no write scope, and changes to that path are still rejected. A phase with only such references gets no write scope and may end with an empty commit.
- `devplane checkpoint` no longer treats the run state Spec Kit writes (`.specify/feature.json`, `.specify/workflows/runs/`) as an unrelated change. It stays out of Git through `.git/info/exclude`, and any other change still blocks the checkpoint.

### Changed

- **Breaking:** `devplane init` and `devplane new` now default to `--runtime claude`.
- **Breaking:** the Hermes adapter and Spec Kit phases no longer default to the `minimax-oauth` provider and `MiniMax-M3` model. Without `provider` and `model`, the agent uses its own configuration. Existing projects that relied on those defaults must set them explicitly.
- `validate`, `run`, `approve`, and `resume` read the phase agent from `.specify/integration.json`, so existing Hermes projects keep using Hermes.
- `.agents/`, `.claude/`, and `.opencode/` are now governed paths that implementation assignments cannot change, as `.hermes/` already was.
- Moved the agent prompts out of `parallel.py` and `pipeline.py` into packaged templates under `devplane/templates`, and translated them to English with the same contract rules.
- Reworded runtime-agnostic docstrings and the managed Spec Kit rules so they no longer present Hermes as the coordinator or the only runtime.

### Removed

- Removed the unused `devplane.minimax_runner` module. Its runner coverage now targets `agent_runtime`, which all execution paths use.

### Security

- Updated locked development dependencies to resolve known vulnerabilities: certifi 2026.10.23, filelock 4.2.6, msgpack 1.2.4, and urllib3 2.8.0.

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

[Unreleased]: https://github.com/dcosodev/devplane/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/dcosodev/devplane/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/dcosodev/devplane/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/dcosodev/devplane/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/dcosodev/devplane/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/dcosodev/devplane/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/dcosodev/devplane/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dcosodev/devplane/releases/tag/v0.1.0
