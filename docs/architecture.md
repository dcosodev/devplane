# Architecture

## Product boundary

DevPlane is a local-first organizational catalog plus an optional development control plane. It does not implement a model, an agent loop, a hosted registry, or an operating-system sandbox.

The architecture keeps four concerns separate:

```text
Catalog                 Workflow producer          Control plane           Runtime adapter
rules + capabilities -> approved task artifacts -> governed assignments -> agent CLI
     neutral                 replaceable               neutral              agent-specific
```

Current implementations:

- catalog: local YAML/Markdown with deterministic resolution;
- workflow producer: GitHub Spec Kit through its Hermes integration, optional;
- control plane: DevPlane plans, worktrees, review, retry, audit, and integration;
- implementation runtimes: Hermes, Claude Code, and OpenCode adapters.

A model such as MiniMax-M3, Claude, or a provider/model selected in OpenCode sits below its agent runtime. It does not define the catalog or execution contract.

## Catalog layer

`AgentProject` points to a local `CapabilityCatalog`. A catalog declares exact capability files and reusable profiles. Capabilities contain:

- command-specific Markdown context;
- declarative write and shell policy;
- trusted validation commands;
- immutable IDs and versions.

The resolver expands one selected profile plus explicit project capabilities. It rejects malformed schemas, duplicate IDs, unavailable versions, missing resources, path escapes, and symlink escapes. It emits deterministic generated files and a source hash over every participating source file.

Workflow and runtime configuration are optional. Therefore `sync`, `validate`, `inspect`, `context`, `profile`, `activate`, and `use-profile` can operate without Spec Kit or an agent executable.

See [`catalog.md`](catalog.md).

## Workflow producer

GitHub Spec Kit remains an external tool. When `workflow.engine: speckit` is selected, DevPlane drives the supported `specify`, `plan`, and `tasks` phases through installed Hermes skills and records explicit human approval between phases.

That integration is not the definition of DevPlane:

- catalog-only projects omit `workflow`;
- execution plans can be produced from a reviewed `tasks.md` without invoking Spec Kit;
- approved tasks are agent-neutral input to the control plane;
- DevPlane never invokes Spec Kit's monolithic implementation phase.

The current Spec Kit phase adapter is Hermes-specific. Adding another workflow producer is separate from adding an implementation runtime adapter.

## Runtime adapter boundary

`src/devplane/agent_runtime.py` defines neutral values:

```text
AgentRuntimeConfig(adapter, executable, provider, model, options)
SessionRequest(task_id, prompt, cwd)
SessionResult(task_id, returncode, stdout, stderr)
```

An adapter converts one request to an argv list. It must not:

- use `shell=True`;
- interpolate a prompt into a shell command;
- silently ignore unknown options;
- bypass runtime permissions;
- push, deploy, or change accounts.

The control plane passes the selected runtime config to both execution paths:

- immutable execution-plan pipeline in `pipeline.py`;
- legacy phase-based compatibility path in `parallel.py`.

Runtime selection changes only the CLI invocation. Assignment semantics, worktrees, review, validation, persisted results, and integration stay unchanged.

See [`runtime-adapters.md`](runtime-adapters.md).

## Execution-plan model

An execution plan is generated before model execution and contains:

- `run_id`;
- approved base commit;
- source tasks path and digest;
- resolved-manifest digest;
- assignments with dependencies and allowed paths;
- exact validation commands.

A SHA-256 sidecar protects the serialized plan. Before execution DevPlane checks the sidecar, plan schema, task digest, manifest digest, base commit, dependency graph, path containment, governance boundaries, and validation commands.

Assignments form an acyclic graph. Setup and foundational work run cumulatively; independent stories may run concurrently from the same verified foundation; polish runs only after successful story consolidation.

## Git and state boundaries

Editable project state:

```text
.devplane/project.yaml
```

Deterministic generated state:

```text
.devplane/generated/resolved-manifest.yaml
.devplane/generated/context-*.md
```

Operational run state is stored under the repository's common Git directory so it is not part of worker commits:

```text
.git/devplane/runs/<run-id>/
```

That directory contains approved-artifact records, execution plans and hashes, result JSON, audit JSONL, and worktree metadata.

Each assignment receives a dedicated branch and worktree. The main project branch remains at the approved checkpoint until explicit integration.

## Independent post-execution review

Agent success output is never accepted as proof. After each session DevPlane verifies:

1. process return code;
2. exactly one focused commit on the expected base;
3. clean worktree;
4. no merge commit or unexpected history;
5. changed files remain inside assignment scope;
6. governance files are unchanged;
7. whitespace checks pass;
8. exact validation commands pass in the reviewed worktree.

Dependent assignments do not start after a failed predecessor. Retry reuses only results whose base and commit remain valid.

Integration requires a clean project branch, the unchanged approved checkpoint, a completed run, and successful final validation. DevPlane then uses `git merge --ff-only`.

## Security boundary

### What DevPlane provides

- safe YAML loading;
- schema and identifier checks;
- path and symlink containment;
- deterministic source hashing;
- argv-array subprocess execution with `shell=False`;
- execution-plan integrity checks;
- Git history and dirty-state checks;
- post-execution path-scope enforcement;
- explicit validation and fast-forward integration;
- non-interactive agent invocations;
- audit records without feature-description plaintext.

### What DevPlane does not provide

- OS process, filesystem, network, or credential isolation;
- syscall-level write prevention;
- a container or VM sandbox;
- cryptographic catalog signatures;
- cryptographically chained audit logs;
- trustworthy behavior from an untrusted agent executable;
- safety for arbitrary validation commands;
- protection against prompt injection in catalog Markdown;
- remote catalog provenance or revocation.

Git worktrees isolate Git branches and files, not the host. Runtime permission systems and the host account remain part of the trusted computing base. A compromised or malicious agent executable can access anything its process identity can access.

Declarative shell and write patterns are policy inputs and generated context. Authoritative write-scope checks occur after execution; they cannot undo external side effects. Use containers or stronger platform sandboxing when pre-execution isolation is required.

Treat catalog YAML, Markdown, project repositories, task descriptions, agent executables, and validation commands as trusted code/configuration. Review third-party catalogs before synchronization.

## Compatibility and extension rules

A new catalog feature must remain meaningful without a runtime. A new runtime adapter must consume the existing neutral request contract and pass adapter-specific tests. An unsupported policy must fail compatibility checks rather than being ignored once capability negotiation is introduced.

A new workflow producer must output reviewed task artifacts or an equivalent neutral task contract. It must not be embedded into catalog semantics.

The `0.2.0` release includes built-in adapters rather than a plugin API. Arbitrary external adapters remain future work because loading executable plugins expands the trust and compatibility surface.

## Intentional alpha limitations

- local catalogs only; no registry or dependency solver;
- one version per capability ID in a catalog;
- one selected profile plus direct capabilities;
- fixed built-in runtime adapter registry;
- Hermes-only Spec Kit phase integration;
- post-execution rather than syscall-level scope enforcement;
- local, non-chained audit JSONL;
- no signed catalogs or plans.
