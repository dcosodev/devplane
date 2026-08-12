from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devplane.commands import run_checked
from devplane.core import DevPlaneError
from devplane.minimax_runner import SessionResult
from devplane.parallel import (
    build_phase_prompt,
    resolve_tasks_file,
    run_parallel_implementation,
)
from devplane.tasks import SpecTask, TaskPhase
from devplane.worktrees import WorktreeHandle


def _phase(name: str = "Phase 3: User Story 1") -> TaskPhase:
    return TaskPhase(
        name=name,
        tasks=[
            SpecTask(
                task_id="T012",
                description="Implement service in src/service.py",
                parallel=True,
                story="US1",
                phase=name,
            )
        ],
    )


def test_resolve_tasks_file_rejects_symlink_escape(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = project / "tasks.md"
    link.symlink_to(outside)

    with pytest.raises(DevPlaneError, match="escapes"):
        resolve_tasks_file(project, link)


def test_build_phase_prompt_contains_governed_contract() -> None:
    prompt = build_phase_prompt(_phase(), "sha256:abc")

    assert "T012" in prompt
    assert "src/service.py" in prompt
    assert "agente escritor" in prompt
    assert "MiniMax" not in prompt
    assert "AGENTS.md" in prompt
    assert "sha256:abc" in prompt
    assert "commit local" in prompt
    assert "No hagas push" in prompt


def test_parallel_implementation_dry_run_does_not_create_worktrees(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    tasks_file = project / "tasks.md"
    tasks_file.write_text(
        "## Phase 3: User Story 1\n- [ ] T012 [P] [US1] Implement service in src/service.py\n",
        encoding="utf-8",
    )
    created: list[str] = []

    result = run_parallel_implementation(
        project,
        tasks_file,
        manifest_digest="sha256:abc",
        max_agents=3,
        dry_run=True,
        create_fn=lambda *args, **kwargs: created.append("called"),
    )

    assert created == []
    assert result.dry_run is True
    assert [item.phase for item in result.phases] == ["Phase 3: User Story 1"]
    assert result.phases[0].status == "planned"


def test_parallel_implementation_launches_user_story_batch_and_records_commits(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    tasks_file = project / "tasks.md"
    tasks_file.write_text(
        "## Phase 3: User Story 1\n- [ ] T012 [P] [US1] Implement A in src/a.py\n"
        "## Phase 4: User Story 2\n- [ ] T013 [P] [US2] Implement B in src/b.py\n",
        encoding="utf-8",
    )
    worktrees: dict[str, WorktreeHandle] = {}

    def create_fn(project, run_id, task_id, base_commit, runner=None):
        path = tmp_path / task_id
        path.mkdir()
        handle = WorktreeHandle(task_id, path, f"devplane/{run_id}/{task_id}", base_commit)
        worktrees[task_id] = handle
        return handle

    batches: list[list[str]] = []

    def sessions_fn(requests, max_workers, runner=None):
        batches.append([request.task_id for request in requests])
        return [
            SessionResult(
                request.task_id,
                0,
                "completed",
                None,
                stderr=f"session_id: sid-{request.task_id}",
            )
            for request in requests
        ]

    def command_runner(args, cwd):
        if args == ["git", "rev-parse", "HEAD"] and cwd == project.resolve():
            return subprocess.CompletedProcess(args, 0, stdout="abcdef0123456789\n")
        if args == ["git", "rev-parse", "HEAD"]:
            suffix = "1" if "story-1" in cwd.name else "2"
            return subprocess.CompletedProcess(args, 0, stdout=f"deadbee{suffix}\n")
        raise AssertionError((args, cwd))

    result = run_parallel_implementation(
        project,
        tasks_file,
        manifest_digest="sha256:abc",
        max_agents=2,
        create_fn=create_fn,
        sessions_fn=sessions_fn,
        command_runner=command_runner,
    )

    assert len(batches) == 1
    assert len(batches[0]) == 2
    assert [item.status for item in result.phases] == ["completed", "completed"]
    assert all(item.commit and item.commit.startswith("deadbee") for item in result.phases)
    assert [item.session_id for item in result.phases] == [
        f"sid-{batches[0][0]}",
        f"sid-{batches[0][1]}",
    ]


def test_parallel_implementation_stops_after_failed_serial_batch(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    tasks_file = project / "tasks.md"
    tasks_file.write_text(
        "## Phase 1: Setup\n- [ ] T001 Setup project\n"
        "## Phase 3: User Story 1\n- [ ] T012 [US1] Implement A in src/a.py\n",
        encoding="utf-8",
    )
    launched: list[list[str]] = []

    def create_fn(project, run_id, task_id, base_commit, runner=None):
        path = tmp_path / task_id
        path.mkdir()
        return WorktreeHandle(task_id, path, f"devplane/{run_id}/{task_id}", base_commit)

    def sessions_fn(requests, max_workers, runner=None):
        launched.append([request.task_id for request in requests])
        return [SessionResult(requests[0].task_id, 1, "", "failed")]

    def command_runner(args, cwd):
        return subprocess.CompletedProcess(args, 0, stdout="abcdef0123456789\n")

    result = run_parallel_implementation(
        project,
        tasks_file,
        manifest_digest="sha256:abc",
        max_agents=2,
        create_fn=create_fn,
        sessions_fn=sessions_fn,
        command_runner=command_runner,
    )

    assert len(launched) == 1
    assert [item.status for item in result.phases] == ["failed", "blocked"]


def test_parallel_implementation_uses_real_git_worktrees(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    tasks_file = project / "tasks.md"
    tasks_file.write_text(
        "## Phase 3: User Story 1\n- [ ] T012 [P] [US1] Implement A in src/a.py\n"
        "## Phase 4: User Story 2\n- [ ] T013 [P] [US2] Implement B in src/b.py\n",
        encoding="utf-8",
    )
    run_checked(["git", "init", "-b", "main"], project)
    run_checked(["git", "config", "user.name", "DevPlane Test"], project)
    run_checked(["git", "config", "user.email", "devplane@test.invalid"], project)
    run_checked(["git", "add", "tasks.md"], project)
    run_checked(["git", "commit", "-m", "test: base"], project)

    def sessions_fn(requests, max_workers, runner=None):
        results = []
        for request in requests:
            generated = request.cwd / f"{request.task_id}.txt"
            generated.write_text(request.task_id + "\n", encoding="utf-8")
            run_checked(["git", "add", generated.name], request.cwd)
            run_checked(["git", "commit", "-m", f"test: {request.task_id}"], request.cwd)
            results.append(
                SessionResult(
                    request.task_id,
                    0,
                    "completed",
                    None,
                    stderr=f"session_id: sid-{request.task_id}",
                )
            )
        return results

    result = run_parallel_implementation(
        project,
        tasks_file,
        manifest_digest="sha256:abc",
        max_agents=2,
        sessions_fn=sessions_fn,
        command_runner=run_checked,
    )

    assert [phase.status for phase in result.phases] == ["completed", "completed"]
    assert all(phase.commit for phase in result.phases)
    assert all(Path(phase.worktree).is_dir() for phase in result.phases if phase.worktree)
    assert {phase.session_id for phase in result.phases} == {
        f"sid-{phase.task_id}" for phase in result.phases
    }


def test_parallel_implementation_records_unexpected_launcher_failure(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    tasks_file = project / "tasks.md"
    tasks_file.write_text(
        "## Phase 3: User Story 1\n- [ ] T012 [US1] Implement A in src/a.py\n",
        encoding="utf-8",
    )

    def create_fn(project, run_id, task_id, base_commit, runner=None):
        path = tmp_path / task_id
        path.mkdir()
        return WorktreeHandle(task_id, path, f"devplane/{run_id}/{task_id}", base_commit)

    def sessions_fn(requests, max_workers, runner=None):
        raise RuntimeError("private failure detail")

    def command_runner(args, cwd):
        return subprocess.CompletedProcess(args, 0, stdout="abcdef0123456789\n")

    result = run_parallel_implementation(
        project,
        tasks_file,
        manifest_digest="sha256:abc",
        max_agents=1,
        create_fn=create_fn,
        sessions_fn=sessions_fn,
        command_runner=command_runner,
    )

    assert result.phases[0].status == "failed"
    assert result.phases[0].error == "session launcher aborted: RuntimeError"
    assert "private failure detail" not in result.phases[0].error
