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

`status` rewrites only the frontmatter `status:` line and refuses any change that would leave the story invalid, for example moving a story without a `write_scope` to `ready`.

## Parallel planning

`plan` considers stories that are `ready` with every dependency `done`:

- Stories in the same batch have pairwise disjoint write scopes, decided exactly by the scope grammar rather than by prefix approximation.
- A story whose scope overlaps another story that is `in_progress` or in `review` is left out of every batch, with the overlap reported under `deferred`.
- A story that overlaps a candidate already in the batch moves to a later batch, again with the reason.
- `--max-parallel` caps a batch; the rest wait in later batches.
- Stories waiting on unfinished dependencies are listed under `waiting`.

The plan is advisory: it does not start agents, create worktrees or claim stories. Declared scopes are what the planner trusts, so an agent that writes outside its scope is still caught by the guard and by the post-run allowed-path check, not by the planner.
