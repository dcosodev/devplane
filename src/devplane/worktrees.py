from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .commands import run_checked
from .core import DevPlaneError

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SAFE_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")


@dataclass(frozen=True)
class WorktreeHandle:
    task_id: str
    path: Path
    branch: str
    base_commit: str


@dataclass(frozen=True)
class WorktreeCleanupPlan:
    run_id: str
    worktrees: tuple[str, ...]
    safe_branches: tuple[str, ...]
    preserved_branches: tuple[str, ...]


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise DevPlaneError(
            f"{label} must be a safe identifier containing only letters, numbers, '_' or '-': {value!r}"
        )


Runner = Callable[[list[str], Path], object]


def _runner_or_default(runner: Runner | None) -> Runner:
    return runner or run_checked


def create_worktree(
    project: Path,
    run_id: str,
    task_id: str,
    base_commit: str,
    runner: Runner | None = None,
) -> WorktreeHandle:
    """Create a parallel worktree after confirming the project has no changes."""
    _validate_identifier(run_id, "run_id")
    _validate_identifier(task_id, "task_id")
    if not isinstance(base_commit, str) or not _SAFE_COMMIT.fullmatch(base_commit):
        raise DevPlaneError("base_commit must be a hexadecimal Git object id")

    project = project.resolve()
    if not project.is_dir():
        raise DevPlaneError(f"project directory not found: {project}")

    worktree_path = (
        project.parent / ".devplane-worktrees" / project.name / run_id / task_id
    ).resolve()
    worktree_root = (
        project.parent / ".devplane-worktrees" / project.name
    ).resolve()
    if not worktree_path.is_relative_to(worktree_root):
        raise DevPlaneError(f"worktree path escapes allowed root: {worktree_path}")

    execute = _runner_or_default(runner)
    status = execute(["git", "status", "--porcelain"], project)
    if getattr(status, "stdout", ""):
        raise DevPlaneError(f"project must have a clean git worktree: {project}")

    branch = f"devplane/{run_id}/{task_id}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    execute(
        [
            "git",
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_path),
            base_commit,
        ],
        project,
    )
    return WorktreeHandle(
        task_id=task_id,
        path=worktree_path,
        branch=branch,
        base_commit=base_commit,
    )


def remove_worktree(
    handle: WorktreeHandle,
    project: Path,
    runner: Runner | None = None,
) -> None:
    """Ask Git to remove a worktree; never remove its files directly."""
    project = project.resolve()
    _validate_identifier(handle.task_id, "task_id")
    worktree_root = (
        project.parent / ".devplane-worktrees" / project.name
    ).resolve()
    path = handle.path.resolve()
    if not path.is_relative_to(worktree_root):
        raise DevPlaneError(f"worktree path escapes allowed root: {path}")
    _runner_or_default(runner)(
        ["git", "worktree", "remove", str(handle.path)],
        project,
    )


def _branch_is_ancestor(project: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", branch, "HEAD"],
        cwd=project,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        shell=False,
    ).returncode == 0


def _branch_patch_is_integrated(project: Path, branch: str) -> bool:
    output = run_checked(["git", "cherry", "HEAD", branch], project).stdout.splitlines()
    return bool(output) and all(line.startswith("-") for line in output)


def _worktree_branches(project: Path) -> dict[Path, str | None]:
    entries: dict[Path, str | None] = {}
    current_path: Path | None = None
    for line in run_checked(["git", "worktree", "list", "--porcelain"], project).stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree ")).resolve()
            entries[current_path] = None
        elif line.startswith("branch ") and current_path is not None:
            entries[current_path] = line.removeprefix("branch ").removeprefix("refs/heads/")
    return entries


def plan_worktree_cleanup(project: Path, run_id: str) -> WorktreeCleanupPlan:
    """Plan cleanup while preserving branches whose changes are not integrated."""
    project = project.expanduser().resolve()
    _validate_identifier(run_id, "run_id")
    prefix = f"devplane/{run_id}/"
    branches = tuple(
        sorted(
            line
            for line in run_checked(
                ["git", "for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}"],
                project,
            ).stdout.splitlines()
            if line.startswith(prefix)
        )
    )
    root = (project.parent / ".devplane-worktrees" / project.name / run_id).resolve()
    worktree_branches = _worktree_branches(project)
    nonremovable = {
        branch
        for path, branch in worktree_branches.items()
        if path.is_relative_to(root)
        and branch is not None
        and run_checked(
            ["git", "status", "--porcelain", "--ignored", "--untracked-files=all"],
            path,
        ).stdout
    }
    safe: list[str] = []
    preserved: list[str] = []
    for branch in branches:
        integrated = _branch_is_ancestor(project, branch) or _branch_patch_is_integrated(
            project, branch
        )
        if integrated and branch not in nonremovable:
            safe.append(branch)
        else:
            preserved.append(branch)
    worktrees = tuple(
        sorted(
            str(path)
            for path, branch in worktree_branches.items()
            if path.is_relative_to(root) and branch in safe
        )
    )
    return WorktreeCleanupPlan(run_id, worktrees, tuple(safe), tuple(preserved))


def apply_worktree_cleanup(project: Path, cleanup: WorktreeCleanupPlan) -> None:
    """Apply an inspected plan after rechecking branch and path safety."""
    project = project.expanduser().resolve()
    if run_checked(["git", "status", "--porcelain"], project).stdout:
        raise DevPlaneError("project must be clean before worktree cleanup")
    current = plan_worktree_cleanup(project, cleanup.run_id)
    if not set(cleanup.safe_branches).issubset(current.safe_branches):
        raise DevPlaneError("cleanup safety changed; generate a fresh dry-run")
    root = (project.parent / ".devplane-worktrees" / project.name / cleanup.run_id).resolve()
    for raw_path in cleanup.worktrees:
        path = Path(raw_path).resolve()
        if not path.is_relative_to(root):
            raise DevPlaneError("cleanup worktree escapes the run root")
        if path.exists():
            run_checked(["git", "worktree", "remove", str(path)], project)
    run_checked(["git", "worktree", "prune"], project)
    for branch in cleanup.safe_branches:
        if not branch.startswith(f"devplane/{cleanup.run_id}/"):
            raise DevPlaneError("cleanup branch escapes the run namespace")
        run_checked(["git", "branch", "-D", branch], project)
    if root.is_dir() and not any(root.iterdir()):
        root.rmdir()
