# Security policy

## Supported versions

DevPlane is alpha software. Security fixes are applied to the latest release and the default branch.

| Version | Supported |
| --- | --- |
| Latest `0.x` release | Yes |
| Older releases | No |

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Include affected versions, reproduction steps, impact, and suggested mitigation.

Do not disclose secrets, exploit details, or sensitive repository contents in a public issue. If private reporting is unavailable, open a minimal public issue asking the maintainer to enable a private contact channel; do not include vulnerability details.

## Security model

DevPlane validates catalog paths, generated-state integrity, execution plans, Git state, changed-file scopes, commits, and post-execution validations. It invokes supported agents with argv arrays and `shell=False`.

DevPlane does not provide an operating-system sandbox. Git worktrees isolate branches and files, not processes, network access, credentials, or the host filesystem. Agent tools can access whatever the host account and runtime permission policy allow. Write-scope enforcement happens after execution rather than at syscall time and cannot undo external side effects.

Catalog YAML and Markdown are trusted configuration and prompt material. They can contain prompt injection. Catalog validation commands are executable contract data. Review external catalogs like source code. Catalogs are source-hashed but not cryptographically signed; local audit JSONL is not cryptographically chained.

Hermes, Claude Code, and OpenCode authentication, provider accounts, models, plugins, and permission policies are outside DevPlane's trust boundary. DevPlane does not bypass runtime permission controls.

Read [`docs/architecture.md#security-boundary`](docs/architecture.md#security-boundary) before using DevPlane with custom catalogs, repositories, prompts, validation commands, or agent executables.
