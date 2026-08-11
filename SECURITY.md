# Security policy

## Supported versions

DevPlane is alpha software. Security fixes are applied to the latest release and the default branch.

| Version | Supported |
| --- | --- |
| Latest `0.x` release | Yes |
| Older releases | No |

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Include affected versions, reproduction steps, impact, and any suggested mitigation.

Do not disclose secrets, exploit details, or sensitive repository contents in a public issue. If private reporting is temporarily unavailable, open a minimal public issue asking the maintainer to enable a private contact channel; do not include vulnerability details.

## Security model

DevPlane validates execution plans, paths, Git state, generated artifacts, and post-execution diffs. It does not provide an operating-system sandbox. Hermes `terminal,file` permissions and Git worktrees reduce accidental interference, while authoritative write-scope enforcement happens after execution.

Read `docs/architecture.md#security-boundary` before using DevPlane with untrusted catalogs, repositories, prompts, or validation commands.
