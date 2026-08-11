# Quick start

This walkthrough exercises DevPlane's governed delivery flow. It creates a separate project and does not modify the DevPlane checkout beyond the local Python environment.

## 1. Install prerequisites

Install Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/). Clone DevPlane and install its test environment:

```bash
git clone https://github.com/dcosodev/devplane.git
cd devplane
uv sync --extra test
uv run devplane --help
```

Install the currently verified GitHub Spec Kit revision separately:

```bash
uv tool install specify-cli --from git+https://github.com/github/spec-kit.git@f36634b5c1463d3592382e863cd5e7b8a94d9c9a
specify --version
```

This revision reports `specify 0.14.5.dev0`. Newer Spec Kit lines may change integration contracts and should be validated before use.

Install and configure [Hermes Agent](https://hermes-agent.nousresearch.com/docs). The `0.1.x` implementation runner expects:

```text
provider: minimax-oauth
model: MiniMax-M3
```

Verify your Hermes and model authentication before starting a paid execution. DevPlane subprocesses are non-interactive, so they intentionally fail instead of waiting for hidden login prompts.

## 2. Verify DevPlane without model calls

```bash
uv run pytest
uv run devplane --help
```

The test suite uses temporary Git repositories and fake `specify`/`hermes` executables. It consumes no model tokens.

## 3. Create a project

From the DevPlane checkout:

```bash
uv run devplane new ../meeting-booking \
  --catalog ./examples/catalog
```

DevPlane initializes Spec Kit with its Hermes integration, resolves the example catalog, creates deterministic generated state, and records a clean Git baseline.

## 4. Generate and approve SDD artifacts

Start only the first phase:

```bash
uv run devplane run \
  "Build a secure meeting booking service" \
  --stop-after tasks \
  --run-id meeting-booking-001 \
  --project ../meeting-booking
```

Inspect the generated specification. Advance exactly one phase at a time:

```bash
uv run devplane approve meeting-booking-001 spec  --project ../meeting-booking
uv run devplane approve meeting-booking-001 plan  --project ../meeting-booking
uv run devplane approve meeting-booking-001 tasks --project ../meeting-booking
```

Each command rejects an invalid state transition. Approval means you reviewed the artifact; it is not a blind continue button.

## 5. Create the execution contract

After checking `tasks.md`, create a focused Git checkpoint:

```bash
uv run devplane checkpoint meeting-booking-001 \
  --tasks-file ../meeting-booking/specs/001-meeting-booking/tasks.md \
  --validation "uv run pytest" \
  --project ../meeting-booking
```

Operational state is stored under:

```text
../meeting-booking/.git/devplane/runs/meeting-booking-001/
```

Review `execution-plan.yaml`. Its sidecar digest, Git base, source task digest, resolved manifest digest, dependencies, allowed paths, and validation commands are checked again before execution.

## 6. Dry-run before model execution

```bash
uv run devplane implement \
  --parallel \
  --execution-plan ../meeting-booking/.git/devplane/runs/meeting-booking-001/execution-plan.yaml \
  --dry-run \
  --project ../meeting-booking
```

Confirm the assignment graph, worktree plan, write scopes, validations, and maximum concurrency.

## 7. Implement and inspect

```bash
uv run devplane implement \
  --parallel \
  --max-agents 3 \
  --execution-plan ../meeting-booking/.git/devplane/runs/meeting-booking-001/execution-plan.yaml \
  --project ../meeting-booking

uv run devplane run-status meeting-booking-001 --project ../meeting-booking
```

The project branch remains at the approved checkpoint while workers operate. DevPlane blocks dependent batches if a worker fails scope, Git, or test review.

Retry only the invalid work:

```bash
uv run devplane retry meeting-booking-001 \
  --max-agents 3 \
  --project ../meeting-booking
```

## 8. Integrate explicitly

```bash
uv run devplane integrate meeting-booking-001 \
  --project ../meeting-booking
```

Integration requires a clean project, the unchanged approved checkpoint, a completed final result, and successful validation in the final worktree. Only then does DevPlane perform `git merge --ff-only`.

## 9. Preview and apply cleanup

```bash
uv run devplane cleanup meeting-booking-001 --project ../meeting-booking
uv run devplane cleanup meeting-booking-001 --apply --project ../meeting-booking
```

Cleanup preserves branches that cannot be proven integrated or patch-equivalent.

## Next steps

- Read [`architecture.md`](architecture.md) before using custom catalogs.
- Adapt the example capability catalog under `examples/catalog/`.
- Use `devplane profile` and `devplane activate` when onboarding an existing repository.
- Keep validation commands native to the target project and review them as executable contract data.
