"""End-to-end tests for ``devplane backlog run`` with a fake agent executable."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devplane.backlog import load_backlog
from devplane.cli import app

runner = CliRunner()

FAKE_HERMES = """#!/usr/bin/env python3
import os
import pathlib
import subprocess
import sys

if "--version" in sys.argv:
    print("0.19.0")
    raise SystemExit(0)
if os.environ.get("DEVPLANE_FAKE_AGENT") == "skip":
    print("agent did nothing", file=sys.stderr)
    raise SystemExit(0)
prompt = sys.argv[sys.argv.index("--query") + 1]
assignment = prompt.split("Assignment: ", 1)[1].splitlines()[0]
scope_block = prompt.split("Allowed write paths:\\n", 1)[1].split("\\n\\nRequired validation", 1)[0]
for line in scope_block.splitlines():
    if not line.startswith("- "):
        continue
    target = pathlib.Path.cwd() / line[2:]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(assignment + "\\n")
    subprocess.run(["git", "add", line[2:]], check=True)
subprocess.run(["git", "commit", "-m", f"test: {assignment}"], check=True, capture_output=True)
print(f"session_id: fake-{assignment}")
"""


def _git(project: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=project, check=True, capture_output=True, text=True
    ).stdout.strip()


def _story(project: Path, story_id: str, scope: str, status: str = "ready") -> None:
    directory = project / "backlog"
    directory.mkdir(exist_ok=True)
    (directory / f"{story_id}.md").write_text(
        f"---\nid: {story_id}\ntitle: Story {story_id}\nstatus: {status}\n"
        f"write_scope: ['{scope}']\n---\nImplement {story_id}.\n",
        encoding="utf-8",
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog = tmp_path / "catalog"
    (catalog / "capabilities").mkdir(parents=True)
    (catalog / "manifest.yaml").write_text(
        "apiVersion: devplane.dev/v1\nkind: CapabilityCatalog\n"
        "metadata: {name: e2e}\nspec: {capabilities: []}\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agent = bin_dir / "hermes"
    agent.write_text(FAKE_HERMES, encoding="utf-8")
    agent.chmod(agent.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    root = tmp_path / "demo"
    created = runner.invoke(
        app, ["new", str(root), "--catalog", str(catalog), "--workflow", "none", "--runtime", "hermes"]
    )
    assert created.exit_code == 0, created.output
    _story(root, "US-001", "src/auth.py")
    _story(root, "US-002", "docs/guide.md")
    _story(root, "US-003", "src/auth.py")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "docs: add stories")
    return root


def _run(project: Path, *args: str):
    return runner.invoke(
        app,
        ["backlog", "run", "--validation", "python3 -c pass", "--project", str(project), *args],
    )


def _run_with(project: Path, command: str, *args: str):
    return runner.invoke(
        app, ["backlog", "run", "--validation", command, "--project", str(project), *args]
    )


def _statuses(project: Path) -> dict[str, str]:
    return {story.id: story.status for story in load_backlog(project).stories}


def test_dry_run_shows_the_batch_and_changes_nothing(project: Path) -> None:
    head = _git(project, "rev-parse", "HEAD")

    result = _run(project, "--dry-run")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["stories"] == ["US-001", "US-002"]
    assert [item["assignment_id"] for item in payload["assignments"]] == ["US-001", "US-002"]
    assert _git(project, "rev-parse", "HEAD") == head
    assert _git(project, "status", "--porcelain") == ""
    assert _statuses(project) == {"US-001": "ready", "US-002": "ready", "US-003": "ready"}
    assert not (project / ".git" / "devplane" / "runs").exists()


def test_run_and_integrate_move_stories_through_the_backlog(project: Path) -> None:
    result = _run(project, "--run-id", "run-e2e", "--max-agents", "2")

    assert result.exit_code == 0, result.output
    outcome = json.loads(result.output)
    assert outcome["status"] == "completed"
    assert _statuses(project) == {"US-001": "in_progress", "US-002": "in_progress", "US-003": "ready"}
    assert _git(project, "log", "-1", "--format=%s") == "chore(backlog): start US-001, US-002"
    assert _git(project, "status", "--porcelain") == ""

    status = runner.invoke(app, ["run-status", "run-e2e", "--project", str(project)])
    assert status.exit_code == 0, status.output
    assert {item["assignment_id"] for item in json.loads(status.output)["assignments"]} == {
        "US-001",
        "US-002",
    }

    integrated = runner.invoke(app, ["integrate", "run-e2e", "--project", str(project)])

    assert integrated.exit_code == 0, integrated.output
    assert json.loads(integrated.output)["backlog_review"] == ["US-001", "US-002"]
    assert _statuses(project) == {"US-001": "review", "US-002": "review", "US-003": "ready"}
    assert _git(project, "log", "-1", "--format=%s") == (
        "chore(backlog): ready for review US-001, US-002"
    )
    tracked = _git(project, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert {"src/auth.py", "docs/guide.md"}.issubset(tracked)
    assert _git(project, "status", "--porcelain") == ""
    assert "co-authored" not in _git(project, "log", "--format=%B").lower()

    # US-003 overlapped US-001, so it becomes plannable only once US-001 is done.
    again = runner.invoke(app, ["backlog", "plan", "--project", str(project)])
    assert json.loads(again.output)["deferred"]["US-003"].startswith("overlaps US-001 (review)")


def test_max_parallel_limits_how_many_stories_are_claimed(project: Path) -> None:
    result = _run(project, "--max-parallel", "1")

    assert result.exit_code == 0, result.output
    assert _statuses(project) == {"US-001": "in_progress", "US-002": "ready", "US-003": "ready"}


def test_run_without_ready_stories_fails_with_the_reasons(project: Path) -> None:
    for story_id in ("US-001", "US-002", "US-003"):
        path = project / "backlog" / f"{story_id}.md"
        path.write_text(path.read_text().replace("status: ready", "status: blocked"))
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "docs: block everything")

    result = _run(project)

    assert result.exit_code == 1
    assert "no ready stories" in result.output


def test_run_refuses_a_dirty_project_and_claims_nothing(project: Path) -> None:
    (project / "notes.txt").write_text("untracked\n", encoding="utf-8")

    result = _run(project)

    assert result.exit_code == 1
    assert "clean" in result.output
    assert _statuses(project) == {"US-001": "ready", "US-002": "ready", "US-003": "ready"}


def test_run_needs_a_validation_command(project: Path) -> None:
    result = runner.invoke(app, ["backlog", "run", "--project", str(project)])

    assert result.exit_code == 1
    assert "validation" in result.output
    assert _statuses(project) == {"US-001": "ready", "US-002": "ready", "US-003": "ready"}
    assert _git(project, "log", "-1", "--format=%s") == "docs: add stories"


def test_story_validation_overrides_a_failing_default_and_needs_no_flag(project: Path) -> None:
    for story_id in ("US-001", "US-002", "US-003"):
        path = project / "backlog" / f"{story_id}.md"
        path.write_text(path.read_text().replace("---\nImplement", "validation: ['python3 -c pass']\n---\nImplement"))
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "docs: story validation")

    own = runner.invoke(app, ["backlog", "run", "--project", str(project)])
    assert own.exit_code == 0, own.output
    assert json.loads(own.output)["status"] == "completed"

    listed = json.loads(runner.invoke(app, ["backlog", "list", "--project", str(project)]).output)
    assert listed[0]["validation"] == ["python3 -c pass"]


def test_story_validation_wins_over_a_default_that_would_fail(project: Path) -> None:
    for story_id in ("US-001", "US-002", "US-003"):
        path = project / "backlog" / f"{story_id}.md"
        path.write_text(path.read_text().replace("---\nImplement", "validation: ['python3 -c pass']\n---\nImplement"))
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "docs: story validation")

    result = _run_with(project, "python3 -c 'import sys; sys.exit(1)'")

    assert result.exit_code == 0, result.output


def test_failed_run_keeps_stories_in_progress_and_can_be_retried(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVPLANE_FAKE_AGENT", "skip")

    failed = _run(project, "--run-id", "run-retry")

    assert failed.exit_code == 1
    assert json.loads(failed.output)["status"] == "failed"
    assert _statuses(project)["US-001"] == "in_progress"
    refused = runner.invoke(app, ["integrate", "run-retry", "--project", str(project)])
    assert refused.exit_code == 1
    assert _statuses(project)["US-001"] == "in_progress"

    monkeypatch.delenv("DEVPLANE_FAKE_AGENT")
    retried = runner.invoke(app, ["retry", "run-retry", "--project", str(project)])
    assert retried.exit_code == 0, retried.output
    integrated = runner.invoke(app, ["integrate", "run-retry", "--project", str(project)])
    assert integrated.exit_code == 0, integrated.output
    assert _statuses(project)["US-001"] == "review"


def test_retry_exits_with_one_when_the_run_still_fails(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVPLANE_FAKE_AGENT", "skip")
    assert _run(project, "--run-id", "run-still-failing").exit_code == 1

    retried = runner.invoke(app, ["retry", "run-still-failing", "--project", str(project)])

    assert retried.exit_code == 1
    assert json.loads(retried.output)["status"] == "failed"


def test_retry_detects_a_story_edited_after_the_run(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVPLANE_FAKE_AGENT", "skip")
    assert _run(project, "--run-id", "run-drift").exit_code == 1
    path = project / "backlog" / "US-001.md"
    path.write_text(path.read_text().replace("Implement US-001.", "Implement something else."))
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "docs: change a story")
    monkeypatch.delenv("DEVPLANE_FAKE_AGENT")

    retried = runner.invoke(app, ["retry", "run-drift", "--project", str(project)])

    assert retried.exit_code == 1
    assert "drift" in retried.output


def test_the_claim_commit_leaves_no_co_author_lines(project: Path) -> None:
    assert _run(project).exit_code == 0

    assert "co-authored" not in _git(project, "log", "-1", "--format=%B").lower()
