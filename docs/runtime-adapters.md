# Runtime adapters

Runtime adapters translate an agent-neutral `SessionRequest(task_id, prompt, cwd)` into one supported CLI invocation. Catalog resolution and execution-plan construction do not depend on an adapter.

## Configuration

```yaml
spec:
  runtime:
    adapter: hermes | claude | opencode
    executable: optional-command-override
    provider: optional-provider
    model: optional-model
    options: {}
```

Legacy `runtime.agent: hermes` remains readable and resolves to the historical `minimax-oauth` / `MiniMax-M3` defaults.

Use the CLI to avoid hand-editing common fields:

```bash
devplane adapters
devplane runtime claude --model sonnet --project ./service
devplane runtime opencode --model openai/gpt-5.3-codex --project ./service
devplane runtime hermes --provider minimax-oauth --model MiniMax-M3 --project ./service
devplane runtime none --project ./catalog-only-project
```

## Hermes

The adapter executes `hermes chat` in quiet, single-query mode. Defaults:

```yaml
provider: minimax-oauth
model: MiniMax-M3
options:
  toolsets: terminal,file
  maxTurns: 80
```

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

## OpenCode

The adapter uses `opencode run --format default`. Models use OpenCode's `provider/model` format. Optional configuration:

```yaml
options:
  agent: build
```

The adapter never passes `--dangerously-skip-permissions`.

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

There is no arbitrary plugin API in `0.2.0`. Adding another runtime requires a reviewed adapter and contract tests. Do not claim compatibility with an agent until its non-interactive CLI contract has been verified.
