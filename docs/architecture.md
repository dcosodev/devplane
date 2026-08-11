# DevPlane architecture

DevPlane separates four responsibilities:

1. GitHub Spec Kit owns `specify`, `plan` and `tasks` semantics and artifacts.
2. DevPlane owns approvals, manifests, execution contracts and lifecycle state.
3. Hermes is the persistent coordinator runtime.
4. Independent `minimax-oauth/MiniMax-M3` sessions write code in isolated Git worktrees.

Spec Kit remains an external dependency; DevPlane does not fork or patch it. Governed SDD runs materialize local one-phase `prompt` workflows under `.git/devplane/runs/<id>/`. Each workflow invokes exactly one installed Hermes skill (`speckit-specify`, `speckit-plan` or `speckit-tasks`) with `minimax-oauth/MiniMax-M3`; the prompt-step contract deliberately uses plain streamed output because Hermes does not expose Spec Kit's legacy `chat --json` flag. `speckit-implement` is never included in those workflows.

## State boundaries

Versioned project state:

- `.devplane/project.yaml`
- `.devplane/generated/resolved-manifest.yaml`
- `.devplane/generated/context-<command>.md`
- `.hermes.md` managed contract
- approved `specs/<feature>/spec.md`, `plan.md` and `tasks.md`

Local operational state:

- `.git/devplane/repository-profile.yaml`
- `.git/devplane/runs/<id>/feature-state.json`
- `.git/devplane/runs/<id>/execution-plan.yaml`
- `.git/devplane/runs/<id>/execution-plan.yaml.sha256`
- `.git/devplane/runs/<id>/result.json`
- `.git/devplane/audit/runs.jsonl`

The feature description is stored separately as `.git/devplane/runs/<id>/request.txt` with mode `0600`; state and audit records contain only its digest. Operational state cannot dirty the working tree or enter a normal commit.

## Governed SDD state machine

```text
run --stop-after tasks
  → /speckit-specify prompt
  → spec_pending_approval

approve spec
  → /speckit-plan prompt
  → plan_pending_approval

approve plan
  → /speckit-tasks prompt
  → tasks_pending_approval

approve tasks
  → ready_to_checkpoint

checkpoint
  → focused artifacts commit
  → execution-plan.yaml
  → ready_to_implement
```

Subprocesses are non-interactive (`stdin=DEVNULL`) and output is captured. This prevents hidden prompts and authentication waits. Human decisions are represented by explicit DevPlane commands rather than an invisible child-process TTY.

## Repository profile and capabilities

`profile` reads local marker files such as `pyproject.toml`, lockfiles, `package.json`, `go.mod` and `Cargo.toml`. It records evidence, not guesses. `profile --approve` writes the exact evidence digest and validation candidates to the project manifest.

Capability selection remains explicit through `activate`. A failed capability resolution restores the original project manifest. DevPlane does not automatically map a detected framework to a corporate capability because that mapping is an architectural decision.

## Execution contract

`tasks.md` is converted into assignments with:

```yaml
id: p3-user-story-1
mode: parallel
tasks: [T004, T005]
dependsOn: [p2-foundational]
allowedPaths: [src/api.py, tests/test_api.py]
validation: [uv run pytest]
allowEmptyCommit: false
```

The plan binds:

- approved base commit;
- semantic tasks digest;
- resolved manifest digest;
- assignment graph and write boundaries;
- validation commands.

Plans are stored with a SHA-256 sidecar. Load-time validation rejects malformed identifiers, unsafe paths, governance paths, duplicate tasks, forward dependencies, missing commands and parallel write overlap. `implement` also recomputes the tasks and manifest digests before launching sessions.

## Cumulative implementation

```text
approved checkpoint
  ↓
Setup writer
  ↓ reviewed commit
Foundational writer based on Setup
  ↓ reviewed commit
User Story writers based on Foundational
  ↓ parallel commits
isolated integration worktree
  ↓ serial cherry-picks + validation
Polish writer based on combined stories
  ↓ reviewed final commit
explicit fast-forward gate
```

The main project branch does not move during implementation. For every assignment DevPlane verifies:

1. session success and sanitized result;
2. one new commit and a clean worktree;
3. changed path safety;
4. every path matches `allowedPaths`;
5. no governance path changed;
6. `git diff --check`;
7. assignment validation commands.

Parallel commits are integrated only in an isolated integration worktree. A failed review, validation or cherry-pick blocks dependent batches and preserves evidence.

## Integration, retry and cleanup

`integrate` requires:

- matching completed plan/result;
- clean project;
- unchanged approved `HEAD`;
- final commit descended from the approved base;
- full distinct validation commands in the final worktree before fast-forward;
- `git merge --ff-only` after the validation gate.

`retry` reuses a prior completed assignment only when its recorded base equals the current cumulative base and it has a valid commit. Failed assignments receive new worktree/branch identifiers. Downstream work is rerun only when reconstruction changes its base.

`cleanup` is dry-run by default. A branch is removable only if it is an ancestor of `HEAD` or `git cherry` proves its patch is already integrated. Non-equivalent branches and their worktrees are preserved. Apply mode rechecks safety before deletion.

## Security boundary

Implemented controls:

- YAML uses `safe_load`.
- Catalog, context, task and state paths are contained and symlink escapes are rejected.
- Git identifiers and object IDs are validated.
- Subprocesses use argument arrays, `shell=False`, `stdin=DEVNULL` and timeouts.
- Execution plans have integrity sidecars and semantic source digests.
- Writer diffs enforce explicit scopes after execution.
- Governance paths are read-only to writers.
- Prompts/model stdout are excluded from persisted pipeline state and audit.
- Greenfield baseline creation refuses `.env` and non-example `.env.*` files.
- Integration cannot silently merge onto a changed project branch.

Remaining limitations:

- Hermes `terminal,file` toolsets are not an OS sandbox. Write-scope enforcement is authoritative post-execution rather than syscall-level prevention.
- Exact coordinator validation commands are user-approved contract data; there is no cross-platform command registry yet.
- Catalog Markdown remains trusted prompt material and catalog signatures are not implemented.
- Audit JSONL is local and append-only by convention, without locking or cryptographic chaining.
- Catalog resolution still has a read-time TOCTOU window if another process mutates the catalog concurrently.
- The legacy full Spec Kit `run`/`resume` compatibility path is retained, but the governed phase flow is the supported path for approvals and parallel implementation.
