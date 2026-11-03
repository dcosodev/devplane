# Story backlog

The backlog is a set of Markdown files under `backlog/` in the project repository. The files are the source of truth, so stories are reviewed in pull requests and do not depend on any agent runtime.

## Story format

One file per story, named `<id>.md` or `<id>-<slug>.md`:

```markdown
---
id: US-001
title: Apply the write scope before writing
status: ready
priority: high
depends_on: []
write_scope:
  - src/devplane/guard.py
  - tests/test_guard.py
validation:
  - uv run pytest tests/test_guard.py
feature: 001-write-scope-guard
---

Free-form Markdown: description and acceptance criteria.
```

| Key | Rules |
| --- | --- |
| `id` | Letters, digits, `_` and `-`; the file name must start with it |
| `title` | Non-empty |
| `status` | `backlog`, `ready`, `in_progress`, `review`, `blocked` or `done` |
| `priority` | `low`, `normal` (default), `high` or `urgent` |
| `depends_on` | Ids of other stories; unknown ids, self-dependencies and cycles are rejected |
| `write_scope` | Scopes in the [bounded glob grammar](#write-scope-grammar); required for `ready`, `in_progress`, `review` and `done` |
| `validation` | Optional list of exact commands that validate this story. They replace the run's default validation for this story only. Each is one line, runs without a shell, and shell operators such as `&&`, `\|` and `;` are rejected |
| `feature` | Optional identifier of the Spec Kit feature the story belongs to |

Unknown keys, symlinked files or directories, files over 256 KiB, duplicate ids and scopes that target a governed root (`.agents`, `.claude`, `.devplane`, `.git`, `.hermes`, `.opencode`, `.specify`, `specs`) are rejected.

### Write scope grammar

Literal path segments, `*` and `?` inside a segment (never crossing `/`), and `**` only as the final segment, matching one or more further segments. Character classes, braces, negation and `**` elsewhere are rejected.

## Statuses

```text
backlog → ready → in_progress → review → done
              ↘      ↓           ↓
               blocked ←─────────┘
```

Allowed moves: `backlog→ready|blocked`, `ready→in_progress|backlog|blocked`, `in_progress→review|ready|blocked`, `review→done|in_progress|blocked`, `blocked→backlog|ready`. `done` is final.

## Commands

All commands accept `--project <root>` and print JSON.

| Command | Purpose |
| --- | --- |
| `devplane backlog check` | Validate every story and the dependency graph |
| `devplane backlog list [--status S]` | List stories |
| `devplane backlog show <id>` | Show one story with its body |
| `devplane backlog next` | Ready stories whose dependencies are done, highest priority first |
| `devplane backlog plan [--max-parallel N]` | Group ready stories into parallel batches |
| `devplane backlog status <id> <new>` | Apply an allowed status change |
| `devplane backlog run [--max-parallel N] [--max-agents N] [--validation CMD] [--batches N --integrate] [--dry-run]` | Claim the next batch and implement it with parallel agents; optionally chain several waves |

`status` rewrites only the frontmatter `status:` line and refuses any change that would leave the story invalid, for example moving a story without a `write_scope` to `ready`.

## Parallel planning

`plan` considers stories that are `ready` with every dependency `done`:

- Stories in the same batch have pairwise disjoint write scopes, decided exactly by the scope grammar rather than by prefix approximation.
- A story whose scope overlaps another story that is `in_progress` or in `review` is left out of every batch, with the overlap reported under `deferred`.
- A story that overlaps a candidate already in the batch moves to a later batch, again with the reason.
- `--max-parallel` caps a batch; the rest wait in later batches.
- Stories waiting on unfinished dependencies are listed under `waiting`.

`plan` is advisory: it does not start agents, create worktrees or claim stories. `run` does that for the first batch. Declared scopes are what the planner trusts, so an agent that writes outside its scope is still caught by the guard and by the post-run allowed-path check, not by the planner.

## Running stories

`devplane backlog run` takes the first batch of `plan` and implements it through the same governed pipeline as `implement --parallel`: one isolated worktree and branch per story, the story's `write_scope` as the assignment's allowed paths, the validation commands, the post-run allowed-path check, and the audit trail.

```bash
devplane backlog run --validation "uv run pytest" --max-parallel 3 --dry-run   # inspect the batch
devplane backlog run --validation "uv run pytest" --max-parallel 3             # claim and implement
devplane run-status <run-id>
devplane integrate <run-id>                                                    # fast-forward the reviewed result
```

What happens:

1. The whole execution plan is validated before anything changes. `--dry-run` stops here and prints the batch.
2. The project must be clean. DevPlane commits `chore(backlog): start <ids>` moving the batch from `ready` to `in_progress`. That commit is the plan's approved base, so every agent worktree sees the claim. If the commit fails, the story files are restored.
3. Agents run in parallel, one per story; the assignment id is the story id.
4. `devplane integrate` fast-forwards the validated result, then commits `chore(backlog): ready for review <ids>` for the stories that are still `in_progress`. Move them to `done` yourself with `devplane backlog status` once reviewed.

If a run fails, the stories stay `in_progress` and `devplane retry <run-id>` reuses the commits that were valid. `implement --execution-plan` and `retry` refuse to continue if a story was edited after the plan was made; status changes alone do not count as drift.

Validation is per story: a story's own `validation` commands are used if it has any, otherwise the `--validation` commands or the catalog's. Stories in one batch are often unrelated, so a single command that fits all of them (for example a test suite that only some of them create) usually does not exist; give each story the command that proves it. A run with no validation for some story is refused before anything is claimed. Editing a story's `validation` after planning counts as drift.

### Chaining waves

By default `run` handles one batch and leaves integration to you. `--batches N --integrate` runs up to N waves in sequence: each wave is claimed, implemented, integrated (the same gate as `devplane integrate`, validation included) and recorded as `review` before the next one is planned, so every wave builds on the integrated result of the previous one. That is why `--batches` above 1 requires `--integrate`.

```bash
devplane backlog run --max-parallel 2 --batches 5 --integrate --run-id sprint
```

Wave run ids are `<run-id>-1`, `<run-id>-2`, and so on. The chain stops at the first failure (a failed wave, a failed integration, or a failed backlog update), when no story is ready, or at the batch limit; the JSON output lists every wave and the reason it stopped. `--integrate` skips the manual review between waves, so use it for work whose validation you trust. Stories that overlap one that is only in `review` wait until it is `done`, and stories whose dependencies are not `done` wait too, so a chain usually covers independent stories.

Limits: one batch per invocation unless chained; the backlog is not updated until `integrate`, so a failed run is never marked as finished; see [Audit trail](#audit-trail).

## Audit trail

The story files and the `chore(backlog): …` commits are the history of what each story contains and when DevPlane moved it. Events are also appended to the local audit log (`.git/devplane/audit/runs.jsonl`, or `.devplane/audit/runs.jsonl` outside a Git repository):

| Event | When | Fields |
| --- | --- | --- |
| `backlog.run.started` | `backlog run` claimed a batch | `run_id`, `base_commit`, `stories`, `manifest_digest` |
| `pipeline.assignment` | each assignment of a run finished | the existing pipeline fields |
| `backlog.status.changed` | `backlog status` moved a story | `story`, `from`, `to` |
| `backlog.review.recorded` | `integrate` moved integrated stories to `review` | `run_id`, `stories`, `final_commit` |

The log is local and not tamper-evident.
