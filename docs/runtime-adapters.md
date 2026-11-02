# Runtime adapters

Runtime adapters translate an agent-neutral `SessionRequest(task_id, prompt, cwd)` into one supported CLI invocation. Catalog resolution and execution-plan construction do not depend on an adapter.

## Configuration

```yaml
spec:
  runtime:
    adapter: claude | codex | hermes | opencode
    executable: optional-command-override
    provider: optional-provider
    model: optional-model
    options: {}
```

Legacy `runtime.agent` remains readable as an alias for `runtime.adapter`. No adapter sets a default model or provider: without them, the agent uses its own configuration.

Use the CLI to avoid hand-editing common fields:

```bash
devplane adapters
devplane runtime claude --model sonnet --project ./service
devplane runtime opencode --model openai/gpt-5.3-codex --project ./service
devplane runtime codex --model your-model --project ./service
devplane runtime hermes --provider your-provider --model your-model --project ./service
devplane runtime none --project ./catalog-only-project
```

## Hermes

The adapter executes `hermes chat` in quiet, single-query mode. Defaults:

```yaml
options:
  toolsets: terminal,file
  maxTurns: 80
```

Set `provider` and `model` to override what your Hermes installation has configured. Other adapters never use them.

The prompt is one argv element. `--source tool` marks sessions as third-party integration work.

## Claude Code

The adapter uses non-interactive `claude --print`, text output, no session persistence, and `acceptEdits` permission mode. It does not pass `--dangerously-skip-permissions`.

Optional scalar settings can be placed in YAML:

```yaml
options:
  permissionMode: acceptEdits
  allowedTools: Read,Edit,Write,Bash
```

Host and managed Claude Code policies remain authoritative. A denied or unavailable tool causes the assignment to fail rather than weakening permissions.

## Codex

The adapter uses non-interactive `codex exec` with the `workspace-write` sandbox. It does not bypass Codex's approvals or sandbox.

```yaml
options:
  sandbox: workspace-write
```

## OpenCode

The adapter uses `opencode run --format default`. Models use OpenCode's `provider/model` format. Optional configuration:

```yaml
options:
  agent: build
```

The adapter never passes `--dangerously-skip-permissions`.

## Spec Kit phases

Each adapter also has a Spec Kit integration, selected with `devplane init` or `devplane new`:

| Agent | Spec Kit installs commands in | DevPlane writes its contract to |
| --- | --- | --- |
| `claude` | `.claude/skills` | `CLAUDE.md` |
| `codex` | `.agents/skills` | `AGENTS.md` |
| `hermes` | `.hermes/skills` | `.hermes.md` |
| `opencode` | `.opencode/commands` | `AGENTS.md` |

The integration defaults to the runtime, or to `claude` with `--runtime none`. `--integration` selects a different one. When the integration and the runtime are the same agent, phases use the runtime's settings. Otherwise they use that agent's defaults above.

## Execution contract

Every supported runtime receives the same assignment contract:

- approved tasks and source digests;
- allowed paths;
- exact validations;
- one focused local commit;
- no push, deployment, account action, or destructive Git operation.

After the CLI exits, DevPlane independently checks the commit, dirty state, changed paths, governed paths, whitespace, and validations. Agent output is evidence, not authority.

## What support means

Automated tests verify argv construction, prompt isolation, runtime selection, concurrency, error conversion, and control-plane wiring. CI does not authenticate to model providers or spend tokens. Real execution therefore also depends on:

- the CLI version installed by the operator;
- valid authentication and model access;
- runtime permission policy;
- repository trust and host permissions.

There is no arbitrary plugin API in `0.3.x`. Adding another runtime requires a reviewed adapter and contract tests. Do not claim compatibility with an agent until its non-interactive CLI contract has been verified.
