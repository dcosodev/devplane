"""End-to-end tests for ``devplane backlog specify`` with stub ``specify`` and agent executables."""

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

FAKE_SPECIFY = """#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys

cwd = pathlib.Path.cwd()
args = sys.argv[1:]
if args and args[0] == "init":
    (cwd / ".specify").mkdir(exist_ok=True)
    (cwd / ".hermes" / "skills").mkdir(parents=True, exist_ok=True)
    (cwd / ".specify" / "integration.json").write_text(json.dumps({
        "version": "0.14.0", "installed_integrations": ["hermes"], "default_integration": "hermes"
    }))
    raise SystemExit(0)
if args[:2] == ["workflow", "run"]:
    workflow = pathlib.Path(args[2]).read_text()
    if os.environ.get("DEVPLANE_FAKE_SPECIFY") == "fail" and "speckit.specify" in workflow:
        print("specify exploded", file=sys.stderr)
        raise SystemExit(2)
    (cwd / ".specify" / "feature.json").write_text("{}")
    (cwd / ".specify" / "workflows" / "runs" / "stub").mkdir(parents=True, exist_ok=True)
    (cwd / ".specify" / "workflows" / "runs" / "stub" / "state.json").write_text("{}")
    feature = cwd / "specs" / "001-demo"
    feature.mkdir(parents=True, exist_ok=True)
    if "speckit.specify" in workflow:
        subprocess.run(["git", "checkout", "-b", "001-demo"], cwd=cwd, check=True, capture_output=True)
        (feature / "spec.md").write_text("# Demo specification\\n")
        phase = "specify"
    elif "speckit.plan" in workflow:
        (feature / "plan.md").write_text("# Demo plan\\n")
        phase = "plan"
    elif "speckit.tasks" in workflow:
        (feature / "tasks.md").write_text(
            "## Phase 1: Setup\\n- [ ] T001 Create setup/generated.txt\\n\\n"
            "## Phase 2: Foundational\\n- [ ] T002 Create foundation/generated.txt\\n"
        )
        phase = "tasks"
    else:
        raise SystemExit("unknown workflow")
    print(json.dumps({"run_id": f"external-{phase}", "status": "completed"}))
    raise SystemExit(0)
print("unsupported specify invocation", file=sys.stderr)
raise SystemExit(2)
"""

FAKE_HERMES = """#!/usr/bin/env python3
import pathlib
import subprocess
import sys

if "--version" in sys.argv:
    print("0.19.0")
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


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _git(project: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=project, check=True, capture_output=True, text=True
    ).stdout.strip()


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
    _executable(bin_dir / "specify", FAKE_SPECIFY)
    _executable(bin_dir / "hermes", FAKE_HERMES)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    root = tmp_path / "demo"
    created = runner.invoke(app, ["new", str(root), "--catalog", str(catalog), "--runtime", "hermes"])
    assert created.exit_code == 0, created.output
    (root / "backlog").mkdir()
    (root / "backlog" / "US-001.md").write_text(
        "---\nid: US-001\ntitle: Sum function\nstatus: ready\nwrite_scope: ['calc/add.py']\n---\n"
        "Add a function that sums two numbers.\n",
        encoding="utf-8",
    )
    (root / "backlog" / "US-002.md").write_text(
        "---\nid: US-002\ntitle: Idea\nstatus: backlog\n---\nNot ready yet.\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "docs: add stories")
    return root


def _specify(project: Path, *args: str):
    return runner.invoke(app, ["backlog", "specify", *args, "--project", str(project)])


def _story(project: Path, story_id: str):
    return load_backlog(project).get(story_id)


def test_specify_starts_the_feature_run_and_links_the_story(project: Path) -> None:
    result = _specify(project, "US-001")

    assert result.exit_code == 0, result.output
    outcome = json.loads(result.output)
    assert outcome["story"] == "US-001"
    assert outcome["feature"] == "spec-US-001"
    assert outcome["status"] == "spec_pending_approval"
    assert outcome["next"] == "devplane approve spec-US-001 spec"
    assert _story(project, "US-001").feature == "spec-US-001"
    assert (project / "specs" / "001-demo" / "spec.md").is_file()
    assert _git(project, "log", "-1", "--format=%s") == "chore(backlog): link US-001 to feature spec-US-001"
    assert _git(project, "show", "--name-only", "--format=", "HEAD").split() == ["backlog/US-001.md"]
    assert "co-authored" not in _git(project, "log", "--format=%B").lower()


def test_the_story_text_becomes_the_feature_description(project: Path) -> None:
    assert _specify(project, "US-001").exit_code == 0

    request = project / ".git" / "devplane" / "runs" / "spec-US-001" / "request.txt"
    description = request.read_text(encoding="utf-8")
    assert description.startswith("Sum function")
    assert "Add a function that sums two numbers." in description


def test_a_custom_run_id_is_used_and_recorded(project: Path) -> None:
    result = _specify(project, "US-001", "--run-id", "sum-feature")

    assert result.exit_code == 0, result.output
    assert _story(project, "US-001").feature == "sum-feature"


def test_a_story_that_is_already_linked_is_not_specified_again(project: Path) -> None:
    assert _specify(project, "US-001").exit_code == 0
    head = _git(project, "rev-parse", "HEAD")

    again = _specify(project, "US-001", "--run-id", "second")

    assert again.exit_code == 1
    assert "already linked" in again.output
    assert _git(project, "rev-parse", "HEAD") == head
    assert not (project / ".git" / "devplane" / "runs" / "second").exists()


def test_an_unknown_story_is_rejected(project: Path) -> None:
    result = _specify(project, "US-404")

    assert result.exit_code == 1
    assert "unknown story" in result.output


def test_a_failed_specify_phase_leaves_the_story_unlinked(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVPLANE_FAKE_SPECIFY", "fail")
    head = _git(project, "rev-parse", "HEAD")

    result = _specify(project, "US-001")

    assert result.exit_code == 1
    assert _story(project, "US-001").feature is None
    assert _git(project, "rev-parse", "HEAD") == head
    assert not (project / ".git" / "devplane" / "runs" / "spec-US-001" / "feature-state.json").exists()


def test_the_board_and_show_report_the_feature_status(project: Path) -> None:
    assert _specify(project, "US-001").exit_code == 0

    board = json.loads(
        runner.invoke(app, ["backlog", "board", "--format", "json", "--project", str(project)]).output
    )
    card = next(item for item in board["columns"]["ready"] if item["id"] == "US-001")
    shown = json.loads(runner.invoke(app, ["backlog", "show", "US-001", "--project", str(project)]).output)
    text = runner.invoke(app, ["backlog", "board", "--project", str(project)]).output

    assert (card["feature"], card["feature_status"]) == ("spec-US-001", "spec_pending_approval")
    assert (shown["feature"], shown["feature_status"]) == ("spec-US-001", "spec_pending_approval")
    assert "[feature spec-US-001: spec_pending_approval]" in text


def test_a_story_without_a_feature_has_no_feature_status(project: Path) -> None:
    shown = json.loads(runner.invoke(app, ["backlog", "show", "US-001", "--project", str(project)]).output)

    assert shown["feature"] is None
    assert shown["feature_status"] is None


def test_the_run_command_still_starts_governed_feature_runs(project: Path) -> None:
    result = runner.invoke(
        app,
        ["run", "Build demo", "--stop-after", "tasks", "--run-id", "plain-run", "--project", str(project)],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "spec_pending_approval"


def test_the_whole_lifecycle_ends_with_the_story_in_review(project: Path) -> None:
    assert _specify(project, "US-001", "--run-id", "life").exit_code == 0
    for artifact in ("spec", "plan", "tasks"):
        approved = runner.invoke(app, ["approve", "life", artifact, "--project", str(project)])
        assert approved.exit_code == 0, approved.output
    tasks_file = project / "specs" / "001-demo" / "tasks.md"
    checkpoint = runner.invoke(
        app,
        ["checkpoint", "life", "--tasks-file", str(tasks_file), "--validation", "python3 -c pass", "--project", str(project)],
    )
    assert checkpoint.exit_code == 0, checkpoint.output
    plan = project / ".git" / "devplane" / "runs" / "life" / "execution-plan.yaml"
    implemented = runner.invoke(
        app,
        ["implement", "--parallel", "--execution-plan", str(plan), "--project", str(project)],
    )
    assert implemented.exit_code == 0, implemented.output

    integrated = runner.invoke(app, ["integrate", "life", "--project", str(project)])

    assert integrated.exit_code == 0, integrated.output
    assert json.loads(integrated.output)["backlog_review"] == ["US-001"]
    assert _story(project, "US-001").status == "review"
    assert _story(project, "US-002").status == "backlog"
    assert _git(project, "log", "-1", "--format=%s") == "chore(backlog): ready for review US-001 (feature life)"
    assert _git(project, "status", "--porcelain") == ""


def test_integrating_a_feature_that_no_story_links_leaves_the_output_unchanged(project: Path) -> None:
    started = runner.invoke(
        app, ["run", "Build demo", "--stop-after", "tasks", "--run-id", "solo", "--project", str(project)]
    )
    assert started.exit_code == 0, started.output
    for artifact in ("spec", "plan", "tasks"):
        assert runner.invoke(app, ["approve", "solo", artifact, "--project", str(project)]).exit_code == 0
    tasks_file = project / "specs" / "001-demo" / "tasks.md"
    assert runner.invoke(
        app,
        ["checkpoint", "solo", "--tasks-file", str(tasks_file), "--validation", "python3 -c pass", "--project", str(project)],
    ).exit_code == 0
    plan = project / ".git" / "devplane" / "runs" / "solo" / "execution-plan.yaml"
    assert runner.invoke(
        app, ["implement", "--parallel", "--execution-plan", str(plan), "--project", str(project)]
    ).exit_code == 0

    integrated = runner.invoke(app, ["integrate", "solo", "--project", str(project)])

    assert integrated.exit_code == 0, integrated.output
    assert "backlog_review" not in json.loads(integrated.output)
    assert _story(project, "US-001").status == "ready"
