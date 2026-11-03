"""Run backlog stories through the governed execution pipeline.

A backlog run reuses the existing execution plan, worktree pipeline,
integration gate and audit trail. This module only translates between the two:

* ``build_backlog_plan`` turns a batch of stories into an execution plan whose
  assignments are the stories (assignment id = story id, allowed paths = the
  story's ``write_scope``);
* ``claim_stories`` moves them to ``in_progress`` in a dedicated commit, which
  becomes the plan's approved base, so every agent worktree sees the claim;
* ``verify_backlog_source`` detects stories edited after the plan was made;
* ``mark_ready_for_review`` records the integrated work in the backlog.

Story status changes are only ever committed while the project HEAD is a known
base, so they never get in the way of the fast-forward integration gate.
"""

from __future__ import annotations

import hashlib
import textwrap
from collections.abc import Sequence
from pathlib import Path

import yaml

from .backlog import BACKLOG_DIRECTORY, Story, load_backlog, set_status
from .commands import run_checked
from .core import DevPlaneError
from .execution_plan import (
    ExecutionAssignment,
    ExecutionPlan,
    validate_execution_plan,
)

BACKLOG_SOURCE = BACKLOG_DIRECTORY


def stories_digest(stories: Sequence[Story]) -> str:
    """Hash the immutable content of stories, in the given order, ignoring status."""
    canonical = [
        {
            "id": story.id,
            "title": story.title,
            "priority": story.priority,
            "depends_on": list(story.depends_on),
            "write_scope": list(story.write_scope),
            "validation": list(story.validation),
            "feature": story.feature,
            "body": story.body,
        }
        for story in stories
    ]
    payload = yaml.safe_dump(canonical, sort_keys=True, allow_unicode=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _task_text(story: Story) -> str:
    title = " ".join(story.title.split())
    body = story.body.strip()
    text = f"{story.id}: {title}"
    return f"{text}\n{textwrap.indent(body, '  ')}" if body else text


def build_backlog_plan(
    stories: Sequence[Story],
    *,
    run_id: str,
    manifest_digest: str,
    base_commit: str,
    validation_commands: Sequence[str],
) -> ExecutionPlan:
    """Create an execution plan with one assignment per story."""
    if not stories:
        raise DevPlaneError("a backlog run needs at least one story")
    if not isinstance(manifest_digest, str) or not manifest_digest.startswith("sha256:"):
        raise DevPlaneError("manifest_digest must be a sha256 digest")
    defaults = tuple(command.strip() for command in validation_commands)
    if any(not command or "\x00" in command for command in defaults):
        raise DevPlaneError("validation commands must be non-empty")
    for story in stories:
        if not story.validation and not defaults:
            raise DevPlaneError(
                f"story {story.id} has no validation command; "
                "pass --validation or add validation to the story"
            )
    mode = "parallel" if len(stories) > 1 else "serial"
    assignments = tuple(
        ExecutionAssignment(
            assignment_id=story.id,
            phase=f"{story.id}: {' '.join(story.title.split())}",
            mode=mode,
            tasks=(_task_text(story),),
            depends_on=(),
            allowed_paths=story.write_scope,
            validation=story.validation or defaults,
            allow_empty_commit=False,
        )
        for story in stories
    )
    plan = ExecutionPlan(
        run_id=run_id,
        source_tasks=BACKLOG_SOURCE,
        tasks_digest=stories_digest(stories),
        manifest_digest=manifest_digest,
        base_commit=base_commit.lower(),
        assignments=assignments,
    )
    validate_execution_plan(plan)
    return plan


def verify_backlog_source(project: Path, plan: ExecutionPlan) -> None:
    """Fail if a story in the plan was edited, or removed, after planning."""
    if plan.source_tasks != BACKLOG_SOURCE:
        raise DevPlaneError("execution plan does not come from the backlog")
    backlog = load_backlog(project)
    stories = [backlog.get(assignment.assignment_id) for assignment in plan.assignments]
    if stories_digest(stories) != plan.tasks_digest:
        raise DevPlaneError("backlog story drift detected")


def _require_clean(project: Path, action: str) -> None:
    if run_checked(["git", "status", "--porcelain"], project).stdout:
        raise DevPlaneError(f"project must be clean before {action}")


def _commit_status(project: Path, stories: Sequence[Story], status: str, message: str) -> None:
    """Move stories to ``status`` and commit; restore everything if that fails."""
    paths = [f"{BACKLOG_DIRECTORY}/{story.path.name}" for story in stories]
    originals = {story.path: story.path.read_text(encoding="utf-8") for story in stories}
    try:
        for story in stories:
            set_status(project, story.id, status)
        run_checked(["git", "add", "--", *paths], project)
        run_checked(["git", "commit", "-m", message], project)
    except BaseException:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")
        try:
            run_checked(["git", "reset", "-q", "--", *paths], project)
        except DevPlaneError:
            pass
        raise


def _head(project: Path) -> str:
    return run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip()


def claim_stories(project: Path, stories: Sequence[Story]) -> str:
    """Commit ``ready -> in_progress`` for ``stories`` and return the new HEAD."""
    project = project.expanduser().resolve()
    _require_clean(project, "claiming stories")
    ids = ", ".join(story.id for story in stories)
    _commit_status(project, stories, "in_progress", f"chore(backlog): start {ids}")
    return _head(project)


def mark_ready_for_review(project: Path, plan: ExecutionPlan) -> list[str]:
    """After integration, move the plan's still in-progress stories to ``review``."""
    project = project.expanduser().resolve()
    backlog = load_backlog(project)
    pending = []
    for assignment in plan.assignments:
        try:
            story = backlog.get(assignment.assignment_id)
        except DevPlaneError:
            continue
        if story.status == "in_progress":
            pending.append(story)
    if not pending:
        return []
    _require_clean(project, "recording the integrated stories")
    ids = ", ".join(story.id for story in pending)
    _commit_status(project, pending, "review", f"chore(backlog): ready for review {ids}")
    return [story.id for story in pending]
