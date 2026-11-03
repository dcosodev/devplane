"""Tests for the pre-execution write guard (Claude Code PreToolUse hook)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from devplane.core import DevPlaneError
from devplane.guard import GuardPolicy, decide, load_policy, write_policy


def _policy(root: Path, *paths: str) -> GuardPolicy:
    return GuardPolicy(root=root.resolve(), allowed_paths=tuple(paths))


def _event(tool: str, **tool_input: Any) -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }


def test_allows_write_inside_scope_with_absolute_path(tmp_path: Path) -> None:
    policy = _policy(tmp_path, "src/service.py")

    decision = decide(
        policy, _event("Edit", file_path=str(tmp_path / "src/service.py"))
    )

    assert decision.allowed is True


def test_allows_relative_path_resolved_against_root(tmp_path: Path) -> None:
    policy = _policy(tmp_path, "src/auth/**")

    decision = decide(policy, _event("Write", file_path="src/auth/a/b.py"))

    assert decision.allowed is True


def test_denies_write_outside_scope(tmp_path: Path) -> None:
    policy = _policy(tmp_path, "src/auth/**")

    decision = decide(
        policy, _event("Write", file_path=str(tmp_path / "src/billing/x.py"))
    )

    assert decision.allowed is False
    assert "src/billing/x.py" in decision.reason


def test_star_does_not_cross_directories(tmp_path: Path) -> None:
    policy = _policy(tmp_path, "src/*.py")

    assert decide(policy, _event("Edit", file_path="src/a.py")).allowed is True
    assert decide(policy, _event("Edit", file_path="src/x/a.py")).allowed is False


@pytest.mark.parametrize(
    "governed", [".devplane/x.md", ".git/config", ".specify/a", "specs/001/tasks.md"]
)
def test_denies_governed_paths_even_when_scope_matches(
    tmp_path: Path, governed: str
) -> None:
    policy = _policy(tmp_path, "**")

    decision = decide(policy, _event("Write", file_path=governed))

    assert decision.allowed is False
    assert "governed" in decision.reason


@pytest.mark.parametrize(
    "governed", [".GIT/config", ".Devplane/x", "Specs/001/tasks.md"]
)
def test_governed_roots_are_matched_case_insensitively(
    tmp_path: Path, governed: str
) -> None:
    policy = _policy(tmp_path, "**")

    assert decide(policy, _event("Write", file_path=governed)).allowed is False


@pytest.mark.parametrize("path", ["../outside.py", "src/../../outside.py"])
def test_denies_paths_that_escape_the_root(tmp_path: Path, path: str) -> None:
    policy = _policy(tmp_path, "**")

    assert decide(policy, _event("Write", file_path=path)).allowed is False


def test_denies_absolute_path_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    policy = _policy(root, "**")

    decision = decide(policy, _event("Write", file_path=str(tmp_path / "elsewhere.py")))

    assert decision.allowed is False


def test_denies_write_through_symlink_that_leaves_the_root(tmp_path: Path) -> None:
    root = tmp_path / "work"
    outside = tmp_path / "outside"
    (root / "src").mkdir(parents=True)
    outside.mkdir()
    os.symlink(outside, root / "src" / "link")
    policy = _policy(root, "src/**")

    decision = decide(policy, _event("Write", file_path="src/link/evil.py"))

    assert decision.allowed is False


def test_denies_write_through_symlink_into_governed_root(tmp_path: Path) -> None:
    root = tmp_path / "work"
    (root / ".git").mkdir(parents=True)
    os.symlink(root / ".git", root / "gitlink")
    policy = _policy(root, "**")

    assert decide(policy, _event("Write", file_path="gitlink/config")).allowed is False


def test_multiedit_and_notebook_edit_are_checked(tmp_path: Path) -> None:
    policy = _policy(tmp_path, "src/**")

    assert decide(policy, _event("MultiEdit", file_path="src/a.py")).allowed is True
    assert decide(policy, _event("MultiEdit", file_path="docs/a.md")).allowed is False
    assert (
        decide(policy, _event("NotebookEdit", notebook_path="src/n.ipynb")).allowed
        is True
    )
    assert (
        decide(policy, _event("NotebookEdit", notebook_path="docs/n.ipynb")).allowed
        is False
    )


@pytest.mark.parametrize(
    "tool_input", [{}, {"file_path": ""}, {"file_path": 3}, {"file_path": None}]
)
def test_write_tools_without_a_usable_path_are_denied(
    tmp_path: Path, tool_input: dict[str, Any]
) -> None:
    policy = _policy(tmp_path, "**")

    assert decide(policy, _event("Edit", **tool_input)).allowed is False


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "Bash", "WebFetch"])
def test_other_tools_are_not_governed_by_the_write_guard(
    tmp_path: Path, tool: str
) -> None:
    policy = _policy(tmp_path, "src/only.py")

    assert (
        decide(policy, _event(tool, command="ls", file_path="/etc/passwd")).allowed
        is True
    )


@pytest.mark.parametrize(
    "event",
    [
        None,
        [],
        "Edit",
        {},
        {"tool_name": 3},
        {"tool_name": "Edit"},
        {"tool_name": "Edit", "tool_input": []},
    ],
)
def test_malformed_events_are_denied(tmp_path: Path, event: Any) -> None:
    policy = _policy(tmp_path, "**")

    assert decide(policy, event).allowed is False


# ---------------------------------------------------------------------------
# policy files
# ---------------------------------------------------------------------------


def test_policy_round_trips_through_a_file(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    policy = _policy(root, "src/**", "tests/test_a.py")
    target = tmp_path / "state" / "guard.json"

    write_policy(target, policy)

    assert load_policy(target) == policy
    assert (target.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        json.dumps({"root": "relative/path", "allowed_paths": ["src/**"]}),
        json.dumps({"root": "/tmp", "allowed_paths": ["src/[ab]"]}),
        json.dumps({"root": "/tmp", "allowed_paths": "src/**"}),
        json.dumps({"allowed_paths": ["src/**"]}),
    ],
)
def test_invalid_policy_files_are_rejected(tmp_path: Path, content: str) -> None:
    target = tmp_path / "guard.json"
    target.write_text(content, encoding="utf-8")

    with pytest.raises(DevPlaneError):
        load_policy(target)


def test_missing_policy_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DevPlaneError):
        load_policy(tmp_path / "absent.json")
