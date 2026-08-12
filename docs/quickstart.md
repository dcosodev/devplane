# Quick start

This walkthrough separates catalog use, runtime selection, and the optional Spec Kit workflow.

## 1. Install DevPlane

Prerequisites: Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/dcosodev/devplane.git
cd devplane
uv sync --extra test
uv run devplane --help
uv run devplane adapters
```

The unit and controlled integration tests use temporary repositories and fake agent executables. They do not consume model tokens:

```bash
uv run pytest
```

## 2. Exercise the catalog without an agent

Initialize an existing directory as a catalog-only project:

```bash
mkdir -p ../catalog-demo
uv run devplane init ../catalog-demo \
  --catalog ./examples/catalog \
  --workflow none \
  --runtime none
```

Select one reusable development profile and inspect its resolved policy:

```bash
uv run devplane use-profile python-service --project ../catalog-demo
uv run devplane sync --project ../catalog-demo
uv run devplane validate --project ../catalog-demo
uv run devplane inspect --project ../catalog-demo
uv run devplane context plan --project ../catalog-demo
uv run devplane context implement --project ../catalog-demo
```

No Spec Kit, Hermes, Claude Code, OpenCode, provider account, or model is required for this path.

## 3. Create an executable project

Check the Git identity that will author the baseline commit:

```bash
git config --get user.name
git config --get user.email
```

Create a greenfield project with one implementation runtime. This example uses Claude Code but the catalog remains unchanged if you choose another adapter:

```bash
uv run devplane new ../meeting-booking \
  --catalog ./examples/catalog \
  --workflow none \
  --runtime claude

uv run devplane use-profile python-service --project ../meeting-booking
uv run devplane runtime claude \
  --model sonnet \
  --project ../meeting-booking
```

Equivalent selections:

```bash
uv run devplane runtime hermes \
  --provider minimax-oauth \
  --model MiniMax-M3 \
  --project ../meeting-booking

uv run devplane runtime opencode \
  --model openai/gpt-5.3-codex \
  --project ../meeting-booking
```

Install and authenticate only the CLI you selected. DevPlane runs agents non-interactively and fails instead of waiting for a hidden login prompt.

Record the reviewed project configuration:

```bash
git -C ../meeting-booking add .devplane
git -C ../meeting-booking commit -m "chore: select governed development profile"
```

## 4. Provide reviewed tasks

Without a workflow producer, create or copy an approved `tasks.md` into the project. DevPlane parses Spec Kit-style task IDs and paths:

```markdown
## Phase 1: Setup

- [ ] T001 Create package scaffolding in pyproject.toml

## Phase 2: User story

- [ ] T002 [P] Implement booking model in src/booking/model.py
- [ ] T003 [P] Test booking model in tests/test_booking.py
```

Commit the reviewed tasks:

```bash
git -C ../meeting-booking add tasks.md
git -C ../meeting-booking commit -m "docs: approve implementation tasks"
```

## 5. Create and review the execution contract

The selected `python-service` profile contributes validation defaults. You may override them with repeated `--validation` options.

```bash
uv run devplane plan-execution \
  --tasks-file tasks.md \
  --run-id meeting-booking-001 \
  --project ../meeting-booking
```

Review:

```text
../meeting-booking/.git/devplane/runs/meeting-booking-001/execution-plan.yaml
```

The plan binds its Git base, tasks digest, resolved catalog digest, assignment graph, allowed paths, and validations. A SHA-256 sidecar protects the serialized plan.

## 6. Dry-run, execute, and inspect

```bash
uv run devplane implement \
  --parallel \
  --execution-plan ../meeting-booking/.git/devplane/runs/meeting-booking-001/execution-plan.yaml \
  --dry-run \
  --project ../meeting-booking
```

Only after reviewing the plan and authenticating the selected agent:

```bash
uv run devplane implement \
  --parallel \
  --max-agents 3 \
  --execution-plan ../meeting-booking/.git/devplane/runs/meeting-booking-001/execution-plan.yaml \
  --project ../meeting-booking

uv run devplane run-status meeting-booking-001 --project ../meeting-booking
```

The project branch remains at the approved base while agents work in dedicated worktrees. Runtime output is not trusted as proof; DevPlane independently verifies commits, changed paths, governed files, dirty state, whitespace, and validations.

Retry only invalid assignments:

```bash
uv run devplane retry meeting-booking-001 \
  --max-agents 3 \
  --project ../meeting-booking
```

## 7. Integrate explicitly

```bash
uv run devplane integrate meeting-booking-001 \
  --project ../meeting-booking
```

Integration requires a clean project, unchanged approved base, completed run, and successful final validation. Only then does DevPlane use `git merge --ff-only`.

Preview cleanup before applying it:

```bash
uv run devplane cleanup meeting-booking-001 --project ../meeting-booking
uv run devplane cleanup meeting-booking-001 --apply --project ../meeting-booking
```

## Optional: human-gated Spec Kit producer

Install the currently verified external revision:

```bash
uv tool install specify-cli --from git+https://github.com/github/spec-kit.git@f36634b5c1463d3592382e863cd5e7b8a94d9c9a
specify --version
```

It reports `specify 0.14.5.dev0` at that revision. Create a project with Spec Kit enabled:

```bash
uv run devplane new ../spec-driven-service \
  --catalog ./examples/catalog \
  --workflow speckit \
  --runtime opencode
```

The currently supported Spec Kit phase integration uses Hermes skills to produce `spec`, `plan`, and `tasks`; OpenCode in this example executes the approved implementation assignments.

```bash
uv run devplane run \
  "Build a secure meeting booking service" \
  --stop-after tasks \
  --run-id booking-001 \
  --project ../spec-driven-service

uv run devplane approve booking-001 spec --project ../spec-driven-service
uv run devplane approve booking-001 plan --project ../spec-driven-service
uv run devplane approve booking-001 tasks --project ../spec-driven-service
```

Approval means a human reviewed the artifact. It is not a blind continue button.

## Before production use

- Read [`catalog.md`](catalog.md), [`runtime-adapters.md`](runtime-adapters.md), and [`architecture.md`](architecture.md).
- Replace the illustrative example catalog with reviewed organizational conventions.
- Pin your catalog repository revision in deployment automation.
- Treat Markdown as prompt material and validations as executable code.
- Add container or platform isolation if worktrees and host permissions are not a sufficient boundary.
