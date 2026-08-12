# Examples

`catalog/` is a generic, illustrative organizational catalog used by documentation and tests. It is not presented as a production or corporate standard.

It demonstrates four composable profiles:

| Profile | Capabilities | Intended example |
| --- | --- | --- |
| `general-development` | `sample-base` | Stack-neutral baseline |
| `python-service` | `sample-base`, `python-quality` | Python service with pytest/Ruff |
| `web-frontend` | `sample-base`, `web-quality` | npm-based frontend |
| `documentation` | `sample-base`, `docs-quality` | Documentation changes |

Catalog-only walkthrough:

```bash
uv run devplane init ../demo-project \
  --catalog ./examples/catalog \
  --workflow none \
  --runtime none
uv run devplane use-profile python-service --project ../demo-project
uv run devplane sync --project ../demo-project
uv run devplane validate --project ../demo-project
```

Agent selection is independent:

```bash
uv run devplane runtime claude --model sonnet --project ../demo-project
# or
uv run devplane runtime opencode --model openai/gpt-5.3-codex --project ../demo-project
# or
uv run devplane runtime hermes --provider minimax-oauth --model MiniMax-M3 --project ../demo-project
```

Replace these capabilities with reviewed organizational conventions. The example's write patterns, shell patterns, and Git worktrees do not provide operating-system isolation.

Catalog YAML and Markdown become generated policy and model prompt material. Validation commands may later execute as trusted plan data. Review catalogs like code and do not use untrusted content.
