"""Tests for turning backlog stories into governed execution runs."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from devplane.agent_runtime import SessionResult
from devplane.backlog import load_backlog, plan_batches
from devplane.backlog_run import (
    BACKLOG_SOURCE,
    build_backlog_plan,
    claim_stories,
    mark_ready_for_review,
    stories_digest,
    verify_backlog_source,
)
from devplane.commands import run_checked
from devplane.core import DevPlaneError
from devplane.execution_plan import load_execution_plan, write_execution_plan
from devplane.pipeline import integrate_pipeline, run_execution_pipeline


def _story(
    project: Path,
    story_id: str,
    *,
    status: str = "ready",
    scope: str | None = "src/a.py",
    title: str | None = None,
    body: str = "Do the thing.\n",
    depends_on: str = "[]",
    validation: str | None = None,
    feature: str | None = None,
) -> Path:
    directory = project / "backlog"
    directory.mkdir(exist_ok=True)
    path = directory / f"{story_id}.md"
    extra = f"validation: {validation}\n" if validation is not None else ""
    extra += f"feature: {feature}\n" if feature is not None else ""
    path.write_text(
        f"---\nid: {story_id}\ntitle: {title or 'Story ' + story_id}\nstatus: {status}\n"
        f"depends_on: {depends_on}\nwrite_scope: {[scope] if scope else []}\n{extra}---\n{body}",
        encoding="utf-8",
    )
    return path


def _git_project(tmp_path: Path) -> Path:
    project = tmp_path / "repo"
    project.mkdir()
    run_checked(["git", "init", "-b", "main"], project)
    run_checked(["git", "config", "user.name", "DevPlane Test"], project)
    run_checked(["git", "config", "user.email", "devplane@test.invalid"], project)
    (project / "README.md").write_text("demo\n", encoding="utf-8")
    run_checked(["git", "add", "-A"], project)
    run_checked(["git", "commit", "-m", "test: baseline"], project)
    return project


def _commit_all(project: Path, message: str = "test: stories") -> str:
    run_checked(["git", "add", "-A"], project)
    run_checked(["git", "commit", "-m", message], project)
    return run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip()


def _stories(project: Path, *ids: str):
    backlog = load_backlog(project)
    return [backlog.get(story_id) for story_id in ids]


def _plan(project: Path, *ids: str, base: str | None = None):
    base = base or run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip()
    return build_backlog_plan(
        _stories(project, *ids),
        run_id="backlog-run",
        manifest_digest="sha256:manifest",
        base_commit=base,
        validation_commands=["python3 -c pass"],
    )


# ---------------------------------------------------------------------------
# plan construction
# ---------------------------------------------------------------------------


def test_stories_become_one_assignment_each(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001", scope="src/auth/**", title="Auth", body="Accept: login works.\n")
    _story(project, "US-002", scope="docs/**", title="Docs")

    plan = _plan(project, "US-001", "US-002")

    assert plan.source_tasks == BACKLOG_SOURCE == "backlog"
    assert plan.run_id == "backlog-run"
    first, second = plan.assignments
    assert [first.assignment_id, second.assignment_id] == ["US-001", "US-002"]
    assert first.phase == "US-001: Auth"
    assert first.mode == second.mode == "parallel"
    assert first.depends_on == second.depends_on == ()
    assert first.allowed_paths == ("src/auth/**",)
    assert first.validation == ("python3 -c pass",)
    assert first.allow_empty_commit is False
    (task,) = first.tasks
    assert task.startswith("US-001: Auth")
    assert "  Accept: login works." in task


def test_a_single_story_runs_serially(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001")

    assert _plan(project, "US-001").assignments[0].mode == "serial"


def test_the_plan_survives_the_execution_plan_loader(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001", scope="src/auth/**")
    _story(project, "US-002", scope="docs/**")
    plan = _plan(project, "US-001", "US-002")
    target = tmp_path / "execution-plan.yaml"

    write_execution_plan(plan, target)

    assert load_execution_plan(target) == plan


def test_overlapping_stories_cannot_share_a_plan(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001", scope="src/auth/**")
    _story(project, "US-002", scope="src/auth/models.py")

    with pytest.raises(DevPlaneError, match="overlapping"):
        _plan(project, "US-001", "US-002")


def test_plans_need_stories_and_validation_commands(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001")
    base = run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip()

    with pytest.raises(DevPlaneError, match="at least one story"):
        build_backlog_plan(
            [], run_id="r", manifest_digest="sha256:m", base_commit=base, validation_commands=["x"]
        )
    with pytest.raises(DevPlaneError, match="validation"):
        build_backlog_plan(
            _stories(project, "US-001"),
            run_id="r",
            manifest_digest="sha256:m",
            base_commit=base,
            validation_commands=[],
        )


def test_a_story_validation_replaces_the_default_for_that_story_only(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001", scope="calc/add.py", validation="['python3 -m unittest', 'python3 -c pass']")
    _story(project, "US-002", scope="docs/HELP.md")

    plan = _plan(project, "US-001", "US-002")

    first, second = plan.assignments
    assert first.validation == ("python3 -m unittest", "python3 -c pass")
    assert second.validation == ("python3 -c pass",)


def test_stories_that_all_define_validation_need_no_default(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001", validation="['python3 -c pass']")
    base = run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip()

    plan = build_backlog_plan(
        _stories(project, "US-001"),
        run_id="r",
        manifest_digest="sha256:m",
        base_commit=base,
        validation_commands=[],
    )

    assert plan.assignments[0].validation == ("python3 -c pass",)


def test_a_story_without_any_validation_names_itself_in_the_error(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001", validation="['python3 -c pass']")
    _story(project, "US-002")
    base = run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip()

    with pytest.raises(DevPlaneError, match="US-002.*validation"):
        build_backlog_plan(
            _stories(project, "US-001", "US-002"),
            run_id="r",
            manifest_digest="sha256:m",
            base_commit=base,
            validation_commands=[],
        )


def test_editing_a_story_validation_counts_as_drift(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    path = _story(project, "US-001", validation="['python3 -c pass']")
    plan = _plan(project, "US-001")

    path.write_text(path.read_text().replace("python3 -c pass", "python3 --version"))

    with pytest.raises(DevPlaneError, match="drift"):
        verify_backlog_source(project, plan)


def test_story_scopes_the_plan_loader_would_reject_fail_early(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001", scope="src/a:b.py")

    with pytest.raises(DevPlaneError, match="write scope"):
        _plan(project, "US-001")


# ---------------------------------------------------------------------------
# digest and drift
# ---------------------------------------------------------------------------


def test_digest_tracks_content_but_not_status(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    path = _story(project, "US-001", body="First.\n")
    before = stories_digest(_stories(project, "US-001"))

    path.write_text(path.read_text().replace("status: ready", "status: in_progress"))
    assert stories_digest(_stories(project, "US-001")) == before

    path.write_text(path.read_text().replace("First.", "Second."))
    assert stories_digest(_stories(project, "US-001")) != before


def test_verify_accepts_an_unchanged_backlog_and_status_changes(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    path = _story(project, "US-001")
    plan = _plan(project, "US-001")

    verify_backlog_source(project, plan)
    path.write_text(path.read_text().replace("status: ready", "status: in_progress"))
    verify_backlog_source(project, plan)


def test_verify_detects_edited_and_removed_stories(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    path = _story(project, "US-001", body="First.\n")
    plan = _plan(project, "US-001")

    path.write_text(path.read_text().replace("First.", "Edited."))
    with pytest.raises(DevPlaneError, match="drift"):
        verify_backlog_source(project, plan)

    path.unlink()
    with pytest.raises(DevPlaneError, match="drift|unknown story"):
        verify_backlog_source(project, plan)


# ---------------------------------------------------------------------------
# claiming and finishing
# ---------------------------------------------------------------------------


def test_claim_commits_in_progress_statuses(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001")
    _story(project, "US-002", scope="docs/**")
    before = _commit_all(project)

    head = claim_stories(project, _stories(project, "US-001", "US-002"))

    assert head != before
    assert run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip() == head
    assert run_checked(["git", "status", "--porcelain"], project).stdout == ""
    assert {story.status for story in _stories(project, "US-001", "US-002")} == {"in_progress"}
    subject = run_checked(["git", "log", "-1", "--format=%s"], project).stdout.strip()
    assert subject == "chore(backlog): start US-001, US-002"
    body = run_checked(["git", "log", "-1", "--format=%B"], project).stdout.lower()
    assert "co-authored" not in body


def test_claim_requires_a_clean_project(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001")
    _commit_all(project)
    (project / "README.md").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(DevPlaneError, match=r"clean.*M README\.md"):
        claim_stories(project, _stories(project, "US-001"))

    assert _stories(project, "US-001")[0].status == "ready"


def test_claim_restores_the_stories_when_the_commit_fails(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    path = _story(project, "US-001")
    _commit_all(project)
    hook = project / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    original = path.read_text()

    with pytest.raises(DevPlaneError):
        claim_stories(project, _stories(project, "US-001"))

    assert path.read_text() == original
    assert run_checked(["git", "status", "--porcelain"], project).stdout == ""


def test_claim_only_moves_ready_stories(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001", status="backlog")
    _commit_all(project)

    with pytest.raises(DevPlaneError, match="transition"):
        claim_stories(project, _stories(project, "US-001"))


def test_mark_ready_for_review_moves_only_stories_still_in_progress(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001", scope="src/a.py")
    _story(project, "US-002", scope="docs/**")
    _commit_all(project)
    claim_stories(project, _stories(project, "US-001", "US-002"))
    plan = _plan(project, "US-001", "US-002")
    blocked = project / "backlog" / "US-002.md"
    blocked.write_text(blocked.read_text().replace("status: in_progress", "status: blocked"))
    _commit_all(project, "chore: someone blocked US-002")

    moved = mark_ready_for_review(project, plan)

    assert moved == ["US-001"]
    statuses = {story.id: story.status for story in load_backlog(project).stories}
    assert statuses == {"US-001": "review", "US-002": "blocked"}
    assert run_checked(["git", "status", "--porcelain"], project).stdout == ""
    subject = run_checked(["git", "log", "-1", "--format=%s"], project).stdout.strip()
    assert subject == "chore(backlog): ready for review US-001"


# ---------------------------------------------------------------------------
# end to end through the existing pipeline
# ---------------------------------------------------------------------------


def test_backlog_batch_runs_through_the_pipeline_and_integrates(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001", scope="src/auth/**")
    _story(project, "US-002", scope="docs/**")
    _story(project, "US-003", scope="src/auth/models.py")
    _commit_all(project)

    batch = plan_batches(load_backlog(project), max_parallel=4).batches[0]
    ids = [story.id for story in batch]
    assert ids == ["US-001", "US-002"]
    base = claim_stories(project, list(batch))
    plan = build_backlog_plan(
        _stories(project, *ids),
        run_id="backlog-e2e",
        manifest_digest="sha256:manifest",
        base_commit=base,
        validation_commands=["python3 -c pass"],
    )
    verify_backlog_source(project, plan)

    def sessions_fn(requests, max_workers, runner=None):
        results = []
        for request in requests:
            scope = next(
                item.allowed_paths[0] for item in plan.assignments if item.assignment_id == request.task_id
            )
            target = request.cwd / scope.removesuffix("/**") / f"{request.task_id}.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("done\n", encoding="utf-8")
            run_checked(["git", "add", "-A"], request.cwd)
            run_checked(["git", "commit", "-m", f"test: {request.task_id}"], request.cwd)
            results.append(SessionResult(request.task_id, 0, "completed", None))
        return results

    result = run_execution_pipeline(
        project, plan, max_agents=2, sessions_fn=sessions_fn, command_runner=run_checked
    )
    assert result.status == "completed"

    integrate_pipeline(project, plan, result)
    assert mark_ready_for_review(project, plan) == ["US-001", "US-002"]

    tracked = run_checked(["git", "ls-tree", "-r", "--name-only", "HEAD"], project).stdout.split()
    assert {"src/auth/US-001.txt", "docs/US-002.txt"}.issubset(tracked)
    statuses = {story.id: story.status for story in load_backlog(project).stories}
    assert statuses == {"US-001": "review", "US-002": "review", "US-003": "ready"}
    assert run_checked(["git", "status", "--porcelain"], project).stdout == ""


# ---------------------------------------------------------------------------
# linking stories to Spec Kit feature runs
# ---------------------------------------------------------------------------

from devplane.backlog_run import link_story_to_feature, mark_feature_stories_review


def test_linking_commits_only_the_story_and_keeps_other_staged_changes(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001")
    _commit_all(project)
    (project / "unrelated.txt").write_text("staged, not mine\n", encoding="utf-8")
    run_checked(["git", "add", "unrelated.txt"], project)

    link_story_to_feature(project, "US-001", "spec-US-001")

    assert _stories(project, "US-001")[0].feature == "spec-US-001"
    subject = run_checked(["git", "log", "-1", "--format=%s"], project).stdout.strip()
    assert subject == "chore(backlog): link US-001 to feature spec-US-001"
    committed = run_checked(["git", "show", "--name-only", "--format=", "HEAD"], project).stdout.split()
    assert committed == ["backlog/US-001.md"]
    assert run_checked(["git", "status", "--porcelain"], project).stdout.strip() == "A  unrelated.txt"
    assert "co-authored" not in run_checked(["git", "log", "-1", "--format=%B"], project).stdout.lower()


def test_linking_is_idempotent_and_refuses_a_different_feature(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001")
    _commit_all(project)
    link_story_to_feature(project, "US-001", "run-a")
    head = run_checked(["git", "rev-parse", "HEAD"], project).stdout

    link_story_to_feature(project, "US-001", "run-a")
    assert run_checked(["git", "rev-parse", "HEAD"], project).stdout == head
    with pytest.raises(DevPlaneError, match="already linked"):
        link_story_to_feature(project, "US-001", "run-b")


def test_linking_restores_the_story_when_the_commit_fails(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    path = _story(project, "US-001")
    _commit_all(project)
    hook = project / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    original = path.read_text()

    with pytest.raises(DevPlaneError):
        link_story_to_feature(project, "US-001", "run-a")

    assert path.read_text() == original
    assert run_checked(["git", "status", "--porcelain"], project).stdout == ""


def test_integrating_a_feature_moves_its_linked_stories_to_review(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001", status="in_progress", scope="src/a.py", feature="run-x")
    _story(project, "US-002", status="ready", scope="src/b.py", feature="run-x")
    _story(project, "US-003", status="backlog", scope=None, feature="run-x")
    _story(project, "US-004", status="in_progress", scope="src/d.py", feature="run-y")
    _story(project, "US-005", status="in_progress", scope="src/e.py")
    _commit_all(project)

    moved = mark_feature_stories_review(project, "run-x", ["src/c.py"])

    assert moved == ["US-001", "US-002", "US-003"]
    statuses = {story.id: story.status for story in load_backlog(project).stories}
    assert statuses == {
        "US-001": "review",
        "US-002": "review",
        "US-003": "review",
        "US-004": "in_progress",
        "US-005": "in_progress",
    }
    stories = {story.id: story for story in load_backlog(project).stories}
    assert stories["US-003"].write_scope == ("src/c.py",)  # taken from the feature run
    assert stories["US-001"].write_scope == ("src/a.py",)  # an existing scope is kept
    subject = run_checked(["git", "log", "-1", "--format=%s"], project).stdout.strip()
    assert subject == "chore(backlog): ready for review US-001, US-002, US-003 (feature run-x)"
    assert run_checked(["git", "status", "--porcelain"], project).stdout == ""


def test_integrating_a_feature_without_linked_stories_changes_nothing(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001", status="ready", scope="src/a.py")
    head = _commit_all(project)

    assert mark_feature_stories_review(project, "run-x") == []
    assert run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip() == head


def test_a_project_without_a_backlog_has_nothing_to_move(tmp_path: Path) -> None:
    project = _git_project(tmp_path)

    assert mark_feature_stories_review(project, "run-x") == []


def test_a_backlog_story_without_scope_needs_the_feature_scope(tmp_path: Path) -> None:
    project = _git_project(tmp_path)
    _story(project, "US-001", status="backlog", scope=None, feature="run-x")
    head = _commit_all(project)

    with pytest.raises(DevPlaneError, match="no write_scope"):
        mark_feature_stories_review(project, "run-x")

    assert _stories(project, "US-001")[0].status == "backlog"
    assert run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip() == head
    assert run_checked(["git", "status", "--porcelain"], project).stdout == ""
