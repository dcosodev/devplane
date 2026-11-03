"""CLI-level tests for ``devplane guard``: exit 2 blocks, anything unexpected blocks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devplane.cli import app
from devplane.guard import GuardPolicy, write_policy

runner = CliRunner()


def _policy_file(tmp_path: Path, *paths: str) -> tuple[Path, Path]:
    root = tmp_path / "work"
    root.mkdir()
    policy = tmp_path / "state" / "guard.json"
    write_policy(policy, GuardPolicy(root=root.resolve(), allowed_paths=tuple(paths)))
    return root, policy


def _event(tool: str, **tool_input: object) -> str:
    return json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
    )


def test_guard_allows_an_in_scope_write(tmp_path: Path) -> None:
    _, policy = _policy_file(tmp_path, "src/**")

    result = runner.invoke(
        app,
        ["guard", "--policy", str(policy)],
        input=_event("Write", file_path="src/a.py"),
    )

    assert result.exit_code == 0
    assert result.output == ""


def test_guard_blocks_an_out_of_scope_write_with_exit_two(tmp_path: Path) -> None:
    _, policy = _policy_file(tmp_path, "src/**")

    result = runner.invoke(
        app,
        ["guard", "--policy", str(policy)],
        input=_event("Write", file_path="docs/a.md"),
    )

    assert result.exit_code == 2
    assert "docs/a.md" in result.output
    assert "outside the allowed write scopes" in result.output


@pytest.mark.parametrize("stdin", ["", "not json", "[]", "x" * 2_000_000])
def test_guard_blocks_unreadable_events(tmp_path: Path, stdin: str) -> None:
    _, policy = _policy_file(tmp_path, "**")

    result = runner.invoke(app, ["guard", "--policy", str(policy)], input=stdin)

    assert result.exit_code == 2


def test_guard_blocks_when_the_policy_is_missing(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["guard", "--policy", str(tmp_path / "absent.json")],
        input=_event("Write", file_path="a.py"),
    )

    assert result.exit_code == 2


def test_guard_blocks_on_unexpected_internal_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, policy = _policy_file(tmp_path, "**")

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("devplane.cli.decide", explode)
    result = runner.invoke(
        app, ["guard", "--policy", str(policy)], input=_event("Write", file_path="a.py")
    )

    assert result.exit_code == 2


def test_module_entry_point_enforces_the_policy_end_to_end(tmp_path: Path) -> None:
    root, policy = _policy_file(tmp_path, "src/**")
    command = [sys.executable, "-m", "devplane", "guard", "--policy", str(policy)]

    allowed = subprocess.run(
        command,
        input=_event("Edit", file_path=str(root / "src/a.py")),
        capture_output=True,
        text=True,
        check=False,
    )
    blocked = subprocess.run(
        command,
        input=_event("Edit", file_path=str(root / "README.md")),
        capture_output=True,
        text=True,
        check=False,
    )

    assert allowed.returncode == 0
    assert blocked.returncode == 2
    assert "README.md" in blocked.stderr
