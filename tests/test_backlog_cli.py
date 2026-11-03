"""CLI tests for ``devplane backlog``."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from devplane.cli import app

runner = CliRunner()


def _write(
    project: Path, story_id: str, *, status: str, scope: str, extra: str = ""
) -> None:
    directory = project / "backlog"
    directory.mkdir(exist_ok=True)
    (directory / f"{story_id}.md").write_text(
        f"---\nid: {story_id}\ntitle: Story {story_id}\nstatus: {status}\n"
        f"write_scope: ['{scope}']\n{extra}---\nBody for {story_id}.\n",
        encoding="utf-8",
    )


def _run(project: Path, *args: str):
    return runner.invoke(app, ["backlog", *args, "--project", str(project)])


def test_check_reports_the_story_count(tmp_path: Path) -> None:
    _write(tmp_path, "US-001", status="ready", scope="src/a.py")

    result = _run(tmp_path, "check")

    assert result.exit_code == 0
    assert json.loads(result.output) == {"stories": 1}


def test_check_fails_with_exit_one_on_an_invalid_backlog(tmp_path: Path) -> None:
    _write(tmp_path, "US-001", status="ready", scope="src/[ab]")

    result = _run(tmp_path, "check")

    assert result.exit_code == 1
    assert "write_scope" in result.output


def test_list_filters_by_status(tmp_path: Path) -> None:
    _write(tmp_path, "US-001", status="ready", scope="src/a.py")
    _write(tmp_path, "US-002", status="done", scope="src/b.py")

    everything = json.loads(_run(tmp_path, "list").output)
    ready = json.loads(_run(tmp_path, "list", "--status", "ready").output)

    assert [item["id"] for item in everything] == ["US-001", "US-002"]
    assert [item["id"] for item in ready] == ["US-001"]
    assert ready[0]["write_scope"] == ["src/a.py"]
    assert "body" not in ready[0]


def test_list_rejects_an_unknown_status_filter(tmp_path: Path) -> None:
    assert _run(tmp_path, "list", "--status", "nonsense").exit_code == 1


def test_show_includes_the_body(tmp_path: Path) -> None:
    _write(tmp_path, "US-001", status="ready", scope="src/a.py")

    shown = json.loads(_run(tmp_path, "show", "US-001").output)

    assert shown["id"] == "US-001"
    assert shown["body"].strip() == "Body for US-001."
    assert _run(tmp_path, "show", "US-404").exit_code == 1


def test_next_lists_ready_unblocked_stories_by_priority(tmp_path: Path) -> None:
    _write(tmp_path, "US-001", status="ready", scope="src/a.py")
    _write(
        tmp_path, "US-002", status="ready", scope="src/b.py", extra="priority: urgent\n"
    )
    _write(
        tmp_path,
        "US-003",
        status="ready",
        scope="src/c.py",
        extra="depends_on: [US-004]\n",
    )
    _write(tmp_path, "US-004", status="in_progress", scope="src/d.py")

    result = json.loads(_run(tmp_path, "next").output)

    assert [item["id"] for item in result] == ["US-002", "US-001"]


def test_plan_reports_batches_deferrals_and_waiting(tmp_path: Path) -> None:
    _write(tmp_path, "US-001", status="ready", scope="src/auth/**")
    _write(tmp_path, "US-002", status="ready", scope="src/auth/models.py")
    _write(tmp_path, "US-003", status="ready", scope="docs/**")

    plan = json.loads(_run(tmp_path, "plan", "--max-parallel", "4").output)

    assert plan["batches"] == [["US-001", "US-003"], ["US-002"]]
    assert "US-001" in plan["deferred"]["US-002"]
    assert plan["waiting"] == {}


def test_plan_rejects_a_non_positive_parallel_limit(tmp_path: Path) -> None:
    assert _run(tmp_path, "plan", "--max-parallel", "0").exit_code == 1


def test_status_moves_a_story_and_rejects_invalid_transitions(tmp_path: Path) -> None:
    _write(tmp_path, "US-001", status="backlog", scope="src/a.py")

    moved = _run(tmp_path, "status", "US-001", "ready")
    refused = _run(tmp_path, "status", "US-001", "done")

    assert moved.exit_code == 0
    assert json.loads(moved.output) == {"id": "US-001", "status": "ready"}
    assert refused.exit_code == 1
    assert "transition" in refused.output
    assert json.loads(_run(tmp_path, "show", "US-001").output)["status"] == "ready"


def _audit(project: Path) -> list[dict]:
    path = project / ".devplane" / "audit" / "runs.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_status_changes_are_recorded_in_the_audit_log(tmp_path: Path) -> None:
    _write(tmp_path, "US-001", status="backlog", scope="src/a.py")

    assert _run(tmp_path, "status", "US-001", "ready").exit_code == 0
    assert _run(tmp_path, "status", "US-001", "done").exit_code == 1

    (event,) = _audit(tmp_path)
    assert event["event"] == "backlog.status.changed"
    assert (event["story"], event["from"], event["to"]) == ("US-001", "backlog", "ready")
    assert event["recorded_at"]


# ---------------------------------------------------------------------------
# create and board
# ---------------------------------------------------------------------------


def test_create_writes_the_story_and_records_it(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "create",
        "--title",
        "Función de suma",
        "--priority",
        "high",
        "--write-scope",
        "calc/add.py",
        "--write-scope",
        "tests/test_add.py",
        "--validation",
        "python3 -c pass",
        "--body",
        "Accept: add(1, 2) == 3",
    )

    assert result.exit_code == 0, result.output
    created = json.loads(result.output)
    assert created == {
        "id": "US-001",
        "path": "backlog/US-001-funcion-de-suma.md",
        "status": "backlog",
    }
    shown = json.loads(_run(tmp_path, "show", "US-001").output)
    assert shown["priority"] == "high"
    assert shown["write_scope"] == ["calc/add.py", "tests/test_add.py"]
    assert shown["validation"] == ["python3 -c pass"]
    assert shown["body"].strip() == "Accept: add(1, 2) == 3"
    event = [item for item in _audit(tmp_path) if item["event"] == "backlog.story.created"]
    assert [(item["story"], item["status"]) for item in event] == [("US-001", "backlog")]


def test_create_supports_dependencies_ready_status_and_a_body_file(tmp_path: Path) -> None:
    assert _run(tmp_path, "create", "--title", "First").exit_code == 0
    body = tmp_path / "body.md"
    body.write_text("Long description\n\n- [ ] criterion\n", encoding="utf-8")

    result = _run(
        tmp_path,
        "create",
        "--title",
        "Second",
        "--status",
        "ready",
        "--write-scope",
        "src/b.py",
        "--depends-on",
        "US-001",
        "--body-file",
        str(body),
        "--feature",
        "feature-1",
    )

    assert result.exit_code == 0, result.output
    shown = json.loads(_run(tmp_path, "show", "US-002").output)
    assert (shown["status"], shown["depends_on"], shown["feature"]) == ("ready", ["US-001"], "feature-1")
    assert "criterion" in shown["body"]


def test_create_rejects_invalid_input_without_writing_anything(tmp_path: Path) -> None:
    result = _run(tmp_path, "create", "--title", "Bad", "--write-scope", "src/[ab]")

    assert result.exit_code == 1
    assert "scope" in result.output
    assert not (tmp_path / "backlog").exists()


def test_create_refuses_both_body_options_at_once(tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text("x", encoding="utf-8")

    result = _run(tmp_path, "create", "--title", "T", "--body", "a", "--body-file", str(body))

    assert result.exit_code == 1
    assert "either" in result.output


def test_board_renders_text_markdown_and_json(tmp_path: Path) -> None:
    _write(tmp_path, "US-001", status="ready", scope="src/a.py")
    _write(tmp_path, "US-002", status="done", scope="src/b.py")

    text = _run(tmp_path, "board")
    markdown = _run(tmp_path, "board", "--format", "markdown")
    machine = _run(tmp_path, "board", "--format", "json")

    assert text.exit_code == 0 and "READY (1)" in text.output and "— runs next" in text.output
    assert "### Done (1)" in markdown.output
    payload = json.loads(machine.output)
    assert payload["next_batch"] == ["US-001"]
    assert [card["id"] for card in payload["columns"]["done"]] == ["US-002"]


def test_board_rejects_an_unknown_format(tmp_path: Path) -> None:
    assert _run(tmp_path, "board", "--format", "html").exit_code == 1
