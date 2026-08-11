from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from devplane.commands import run_checked
from devplane.core import DevPlaneError
from devplane.execution_plan import build_execution_plan
from devplane.minimax_runner import SessionResult
from devplane.pipeline import (
    _assignment_prompt,
    integrate_pipeline,
    load_pipeline_run,
    run_execution_pipeline,
    write_pipeline_run,
)
from devplane.tasks import parse_tasks_markdown
from devplane.worktrees import (
    apply_worktree_cleanup,
    create_worktree,
    plan_worktree_cleanup,
)

TASKS = """## Phase 1: Setup
- [ ] T001 Create setup/generated.txt

## Phase 2: Foundational
- [ ] T002 Create foundation/generated.txt

## Phase 3: User Story 1
- [ ] T003 [US1] Create alpha/generated.txt

## Phase 4: User Story 2
- [ ] T004 [US2] Create beta/generated.txt

## Phase 5: Polish
- [ ] T005 Create polish/generated.txt
"""


def test_assignment_prompt_allows_an_auditable_empty_commit_for_validation_only_tasks() -> None:
    plan = build_execution_plan(
        parse_tasks_markdown("## Phase 1: Foundational\n- [ ] T001 Verify src/__init__.py\n"),
        run_id="validation-only",
        source_tasks="specs/001/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0",
        validation_commands=["python3 -m unittest"],
    )

    prompt = _assignment_prompt(plan.assignments[0], plan)

    assert "git commit --allow-empty" in prompt
    assert "solo si la tarea es exclusivamente de validación" in prompt
    assert "No combines comandos con operadores de shell" in prompt

    implementation_plan = build_execution_plan(
        parse_tasks_markdown("## Phase 1: Setup\n- [ ] T001 Create src/service.py\n"),
        run_id="implementation",
        source_tasks="specs/001/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0",
        validation_commands=["python3 -m unittest"],
    )
    assert "git commit --allow-empty" not in _assignment_prompt(
        implementation_plan.assignments[0], implementation_plan
    )


def _git_project(tmp_path: Path, tasks: str = TASKS) -> tuple[Path, str]:
    project = tmp_path / "repo"
    project.mkdir()
    (project / "tasks.md").write_text(tasks, encoding="utf-8")
    run_checked(["git", "init", "-b", "main"], project)
    run_checked(["git", "config", "user.name", "DevPlane Test"], project)
    run_checked(["git", "config", "user.email", "devplane@test.invalid"], project)
    run_checked(["git", "add", "tasks.md"], project)
    run_checked(["git", "commit", "-m", "test: approved tasks"], project)
    base = run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip()
    return project, base


@pytest.mark.parametrize(
    ("task", "expected_status"),
    [
        ("Verify src/__init__.py", "completed"),
        ("Create src/service.py", "failed"),
    ],
)
def test_execution_pipeline_accepts_empty_commits_only_when_the_plan_allows_them(
    tmp_path: Path, task: str, expected_status: str
) -> None:
    tasks = f"## Phase 1: Validation\n- [ ] T001 {task}\n"
    project, base = _git_project(tmp_path, tasks)
    plan = build_execution_plan(
        parse_tasks_markdown(tasks),
        run_id="empty-commit",
        source_tasks="tasks.md",
        manifest_digest="sha256:manifest",
        base_commit=base,
        validation_commands=["python3 -c pass"],
    )

    def sessions_fn(requests, max_workers, runner=None):
        request = requests[0]
        run_checked(["git", "commit", "--allow-empty", "-m", "test: validation checkpoint"], request.cwd)
        return [SessionResult(request.task_id, 0, "completed", None)]

    result = run_execution_pipeline(
        project,
        plan,
        max_agents=1,
        sessions_fn=sessions_fn,
    )

    assert result.status == expected_status
    assert result.assignments[0].changed_paths == ()


def test_execution_pipeline_uses_cumulative_bases_and_leaves_main_untouched(tmp_path: Path) -> None:
    project, base = _git_project(tmp_path)
    plan = build_execution_plan(
        parse_tasks_markdown(TASKS),
        run_id="pipeline-1",
        source_tasks="tasks.md",
        manifest_digest="sha256:manifest",
        base_commit=base,
        validation_commands=["python3 -c pass"],
    )
    created: list[tuple[str, str]] = []

    def create_fn(project, run_id, task_id, base_commit, runner=None):
        created.append((task_id, base_commit))
        return create_worktree(project, run_id, task_id, base_commit, runner=runner)

    path_by_assignment = {
        assignment.assignment_id: assignment.allowed_paths[0]
        for assignment in plan.assignments
    }

    def sessions_fn(requests, max_workers, runner=None):
        results = []
        for request in requests:
            target = request.cwd / path_by_assignment[request.task_id]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(request.task_id + "\n", encoding="utf-8")
            run_checked(["git", "add", target.relative_to(request.cwd).as_posix()], request.cwd)
            run_checked(["git", "commit", "-m", f"test: {request.task_id}"], request.cwd)
            results.append(SessionResult(request.task_id, 0, "completed", None))
        return results

    result = run_execution_pipeline(
        project,
        plan,
        max_agents=2,
        create_fn=create_fn,
        sessions_fn=sessions_fn,
        command_runner=run_checked,
    )

    assert result.status == "completed"
    assert result.base_commit == base
    assert result.final_commit != base
    assert run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip() == base
    files = run_checked(
        ["git", "ls-tree", "-r", "--name-only", result.final_commit], project
    ).stdout.splitlines()
    assert {
        "setup/generated.txt",
        "foundation/generated.txt",
        "alpha/generated.txt",
        "beta/generated.txt",
        "polish/generated.txt",
    }.issubset(files)

    bases = dict(created)
    setup, foundational, us1, us2, polish = plan.assignments
    assert bases[setup.assignment_id] == base
    assert bases[foundational.assignment_id] == result.assignments[0].commit
    assert bases[us1.assignment_id] == result.assignments[1].commit
    assert bases[us2.assignment_id] == result.assignments[1].commit
    assert bases[polish.assignment_id] not in {
        base,
        result.assignments[1].commit,
        result.assignments[2].commit,
        result.assignments[3].commit,
    }


def test_execution_pipeline_rejects_commit_outside_assignment_scope(tmp_path: Path) -> None:
    tasks = (
        "## Phase 3: User Story 1\n"
        "- [ ] T001 [US1] Create allowed/file.txt\n"
        "## Phase 4: Polish\n"
        "- [ ] T002 Update docs/README.md\n"
    )
    project, base = _git_project(tmp_path, tasks)
    plan = build_execution_plan(
        parse_tasks_markdown(tasks),
        run_id="pipeline-2",
        source_tasks="tasks.md",
        manifest_digest="sha256:manifest",
        base_commit=base,
        validation_commands=["python3 -c pass"],
    )

    def sessions_fn(requests, max_workers, runner=None):
        request = requests[0]
        target = request.cwd / "forbidden.txt"
        target.write_text("bad\n", encoding="utf-8")
        run_checked(["git", "add", "forbidden.txt"], request.cwd)
        run_checked(["git", "commit", "-m", "test: escape scope"], request.cwd)
        return [SessionResult(request.task_id, 0, "completed", None)]

    result = run_execution_pipeline(
        project,
        plan,
        max_agents=1,
        sessions_fn=sessions_fn,
        command_runner=run_checked,
    )

    assert result.status == "failed"
    assert result.assignments[0].status == "failed"
    assert "outside allowed paths" in (result.assignments[0].error or "")
    assert result.assignments[1].status == "blocked"
    assert run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip() == base


def test_retry_reuses_valid_completed_commits_and_reruns_failed_path(tmp_path: Path) -> None:
    project, base = _git_project(tmp_path)
    plan = build_execution_plan(
        parse_tasks_markdown(TASKS),
        run_id="pipeline-retry",
        source_tasks="tasks.md",
        manifest_digest="sha256:manifest",
        base_commit=base,
        validation_commands=["python3 -c pass"],
    )
    path_by_assignment = {
        assignment.assignment_id: assignment.allowed_paths[0]
        for assignment in plan.assignments
    }

    def successful(requests, max_workers, runner):
        results = []
        for request in requests:
            target_path = request.cwd / path_by_assignment[request.task_id]
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text("ok\n", encoding="utf-8")
            run_checked(["git", "add", target_path.relative_to(request.cwd).as_posix()], request.cwd)
            run_checked(["git", "commit", "-m", f"test: {request.task_id}"], request.cwd)
            results.append(SessionResult(request.task_id, 0, "completed", None))
        return results

    target = next(item.assignment_id for item in plan.assignments if item.mode == "parallel")
    first_calls: list[str] = []

    def fail_target(requests, max_workers, runner):
        results = []
        for request in requests:
            first_calls.append(request.task_id)
            if request.task_id == target:
                results.append(SessionResult(request.task_id, 1, "", "simulated failure"))
            else:
                results.extend(successful([request], 1, runner))
        return results

    first = run_execution_pipeline(
        project,
        plan,
        max_agents=2,
        create_fn=create_worktree,
        sessions_fn=fail_target,
        command_runner=run_checked,
    )
    assert first.status == "failed"
    completed_before = {
        item.assignment_id for item in first.assignments if item.status == "completed"
    }
    assert completed_before
    retry_calls: list[str] = []

    def retry_sessions(requests, max_workers, runner):
        retry_calls.extend(request.task_id for request in requests)
        return successful(requests, max_workers, runner)

    retried = run_execution_pipeline(
        project,
        plan,
        max_agents=2,
        create_fn=create_worktree,
        sessions_fn=retry_sessions,
        command_runner=run_checked,
        prior_result=first,
    )

    assert retried.status == "completed"
    assert target in retry_calls
    assert completed_before.isdisjoint(retry_calls)
    assert all(item.status == "completed" for item in retried.assignments)


def test_integrate_pipeline_fast_forwards_only_from_approved_base(tmp_path: Path) -> None:
    project, base = _git_project(tmp_path)
    plan = build_execution_plan(
        parse_tasks_markdown(TASKS),
        run_id="pipeline-3",
        source_tasks="tasks.md",
        manifest_digest="sha256:manifest",
        base_commit=base,
        validation_commands=["python3 -c pass"],
    )
    path_by_assignment = {
        assignment.assignment_id: assignment.allowed_paths[0]
        for assignment in plan.assignments
    }

    def sessions_fn(requests, max_workers, runner=None):
        results = []
        for request in requests:
            target = request.cwd / path_by_assignment[request.task_id]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("ok\n", encoding="utf-8")
            run_checked(["git", "add", target.relative_to(request.cwd).as_posix()], request.cwd)
            run_checked(["git", "commit", "-m", f"test: {request.task_id}"], request.cwd)
            results.append(SessionResult(request.task_id, 0, "completed", None))
        return results

    result = run_execution_pipeline(
        project,
        plan,
        max_agents=2,
        sessions_fn=sessions_fn,
        command_runner=run_checked,
    )

    integrated = integrate_pipeline(project, plan, result, command_runner=run_checked)

    assert integrated == result.final_commit
    assert run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip() == result.final_commit

    state_path = project / ".git" / "devplane" / "runs" / plan.run_id / "result.json"
    write_pipeline_run(result, state_path)
    assert load_pipeline_run(state_path) == result

    cleanup = plan_worktree_cleanup(project, plan.run_id)
    assert cleanup.worktrees
    assert cleanup.safe_branches
    assert cleanup.preserved_branches == ()
    apply_worktree_cleanup(project, cleanup)
    assert not (project.parent / ".devplane-worktrees" / project.name / plan.run_id).exists()
    branches = run_checked(
        ["git", "for-each-ref", "--format=%(refname:short)", f"refs/heads/devplane/{plan.run_id}/"],
        project,
    ).stdout
    assert branches == ""


def test_cleanup_dry_run_preserves_worktrees_with_ignored_files(tmp_path: Path) -> None:
    project, base = _git_project(tmp_path)
    handle = create_worktree(project, "cleanup-dirty", "validation", base)
    exclude = project / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text(encoding="utf-8") + "\ncache/\n", encoding="utf-8")
    cache = handle.path / "cache" / "runtime.bin"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"runtime")

    cleanup = plan_worktree_cleanup(project, "cleanup-dirty")

    assert handle.branch in cleanup.preserved_branches
    assert handle.branch not in cleanup.safe_branches
    assert str(handle.path) not in cleanup.worktrees


def test_integrate_pipeline_validates_before_moving_project_head(tmp_path: Path) -> None:
    project, base = _git_project(tmp_path)
    plan = build_execution_plan(
        parse_tasks_markdown("## Phase 1: Setup\n- [ ] T001 Create setup/generated.txt\n"),
        run_id="pipeline-validation-gate",
        source_tasks="tasks.md",
        manifest_digest="sha256:manifest",
        base_commit=base,
        validation_commands=["python3 -c pass"],
    )

    def sessions_fn(requests, max_workers, runner=None):
        request = requests[0]
        target = request.cwd / "setup" / "generated.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("ok\n", encoding="utf-8")
        run_checked(["git", "add", "setup/generated.txt"], request.cwd)
        run_checked(["git", "commit", "-m", "test: setup"], request.cwd)
        return [SessionResult(request.task_id, 0, "completed", None)]

    result = run_execution_pipeline(
        project,
        plan,
        max_agents=1,
        sessions_fn=sessions_fn,
        command_runner=run_checked,
    )
    failing_plan = replace(
        plan,
        assignments=tuple(
            replace(item, validation=("python3 -c 'raise SystemExit(1)'",))
            for item in plan.assignments
        ),
    )

    with pytest.raises(DevPlaneError):
        integrate_pipeline(project, failing_plan, result, command_runner=run_checked)

    assert run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip() == base
