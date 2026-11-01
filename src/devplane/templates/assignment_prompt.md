Work as a writer agent inside an isolated git worktree.

Assignment: {assignment_id}
Phase: {phase}
Tasks:
{tasks}

Allowed write paths:
{scopes}

Required validation:
{validation}

Contract:
1. Read AGENTS.md, `.devplane/generated/context-implement.md`, and the applicable spec.md, plan.md, and tasks.md before editing.
2. Do not write outside the allowed paths; DevPlane will reject the commit.
3. Follow TDD and run the specified validation.
4. Do not modify .devplane/, .git/, .hermes/, .specify/, or specs/.
5. Do not push, deploy, change accounts, or run destructive Git operations.
6. Create a single focused local commit and leave the worktree clean.
{final_contract_rules}

Manifest digest: {manifest_digest}
Tasks digest: {tasks_digest}
