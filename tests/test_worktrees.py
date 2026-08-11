from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devplane.core import DevPlaneError
from devplane.worktrees import WorktreeHandle, create_worktree, remove_worktree


class RecordingRunner:
    def __init__(self, outputs: list[str] | None = None) -> None:
        self.outputs = iter(outputs or [])
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, cwd))
        return subprocess.CompletedProcess(args, 0, stdout=next(self.outputs, ""))


def test_create_worktree_requires_clean_git_project(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    runner = RecordingRunner([" M src/app.py\n"])

    with pytest.raises(DevPlaneError, match="clean"):
        create_worktree(project, "run-1", "task-1", "abc1234", runner=runner)

    assert runner.calls == [(["git", "status", "--porcelain"], project.resolve())]


def test_create_worktree_returns_handle_and_uses_argv(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    runner = RecordingRunner([""])

    handle = create_worktree(project, "run-1", "task_2", "abc1234", runner=runner)

    expected_path = tmp_path / ".devplane-worktrees" / "repo" / "run-1" / "task_2"
    assert handle == WorktreeHandle(
        task_id="task_2",
        path=expected_path,
        branch="devplane/run-1/task_2",
        base_commit="abc1234",
    )
    assert runner.calls == [
        (["git", "status", "--porcelain"], project.resolve()),
        (
            [
                "git",
                "worktree",
                "add",
                "-b",
                "devplane/run-1/task_2",
                str(expected_path),
                "abc1234",
            ],
            project.resolve(),
        ),
    ]


@pytest.mark.parametrize(
    ("run_id", "task_id"),
    [
        ("../escape", "task"),
        ("run", "../../escape"),
        ("run/name", "task"),
        ("run", "task/name"),
        ("", "task"),
        ("run", ""),
        ("--option", "task"),
        ("run", "."),
        ("run", "task name"),
    ],
)
def test_create_worktree_rejects_unsafe_identifiers(
    tmp_path: Path, run_id: str, task_id: str
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    runner = RecordingRunner()

    with pytest.raises(DevPlaneError, match="identifier"):
        create_worktree(project, run_id, task_id, "abc1234", runner=runner)

    assert runner.calls == []
    assert not (tmp_path / ".devplane-worktrees").exists()


@pytest.mark.parametrize("base_commit", ["", "--detach", "HEAD;rm", "../main"])
def test_create_worktree_rejects_unsafe_base_commit(
    tmp_path: Path, base_commit: str
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    runner = RecordingRunner()

    with pytest.raises(DevPlaneError, match="base_commit"):
        create_worktree(project, "run-1", "task-1", base_commit, runner=runner)

    assert runner.calls == []


def test_remove_worktree_delegates_to_git_without_deleting(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    path = tmp_path / ".devplane-worktrees" / "repo" / "run-1" / "task-1"
    handle = WorktreeHandle("task-1", path, "devplane/run-1/task-1", "abc1234")
    runner = RecordingRunner()

    remove_worktree(handle, project, runner=runner)

    assert runner.calls == [
        (["git", "worktree", "remove", str(path)], project.resolve())
    ]


def test_remove_worktree_rejects_path_outside_managed_root(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    outside = tmp_path / "user-data"
    handle = WorktreeHandle(
        "task-1", outside, "devplane/run-1/task-1", "abc1234"
    )
    runner = RecordingRunner()

    with pytest.raises(DevPlaneError, match="escapes"):
        remove_worktree(handle, project, runner=runner)

    assert runner.calls == []
