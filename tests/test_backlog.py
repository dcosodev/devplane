"""Tests for the repository-scoped story backlog."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from devplane.backlog import (
    check_transition,
    load_backlog,
    plan_batches,
    ready_stories,
    set_status,
)
from devplane.core import DevPlaneError


def _story(
    project: Path,
    story_id: str,
    *,
    status: str = "backlog",
    depends_on: list[str] | None = None,
    scope: list[str] | None = None,
    priority: str = "normal",
    title: str | None = None,
    filename: str | None = None,
    extra: str = "",
    body: str = "Body text.\n",
) -> Path:
    directory = project / "backlog"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"id: {story_id}",
        f"title: {title if title is not None else 'Story ' + story_id}",
        f"status: {status}",
        f"priority: {priority}",
        f"depends_on: {depends_on if depends_on is not None else []}",
    ]
    if scope is not None:
        lines.append(f"write_scope: {scope}")
    if extra:
        lines.append(extra)
    lines += ["---", body]
    path = directory / (filename or f"{story_id}.md")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# loading and validation
# ---------------------------------------------------------------------------


def test_project_without_a_backlog_directory_has_no_stories(tmp_path: Path) -> None:
    assert load_backlog(tmp_path).stories == ()


def test_load_parses_stories_sorted_by_id(tmp_path: Path) -> None:
    _story(tmp_path, "US-002", scope=["src/b.py"], status="ready", priority="high")
    _story(tmp_path, "US-001", scope=["src/a/**"], filename="US-001-first-story.md")

    backlog = load_backlog(tmp_path)

    assert [story.id for story in backlog.stories] == ["US-001", "US-002"]
    first, second = backlog.stories
    assert first.write_scope == ("src/a/**",)
    assert first.status == "backlog"
    assert first.path.name == "US-001-first-story.md"
    assert second.priority == "high"
    assert second.body.strip() == "Body text."
    assert backlog.get("US-002") is second


@pytest.mark.parametrize(
    "extra",
    ["unknown_key: 1", "owner: me"],
)
def test_unknown_frontmatter_keys_are_rejected(tmp_path: Path, extra: str) -> None:
    _story(tmp_path, "US-001", extra=extra)

    with pytest.raises(DevPlaneError, match="unknown"):
        load_backlog(tmp_path)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"status": "doing"}, "status"),
        ({"priority": "asap"}, "priority"),
        ({"title": "''"}, "title"),
        ({"depends_on": ["US-404"]}, "unknown story"),
        ({"depends_on": ["US-001"]}, "itself"),
        ({"scope": ["src/[ab]"]}, "scope"),
        ({"scope": ["../x"]}, "scope"),
        ({"scope": ["specs/**"], "status": "ready"}, "governed"),
        ({"scope": [".claude/commands/**"], "status": "ready"}, "governed"),
        ({"scope": [".GIT/hooks/x"], "status": "ready"}, "governed"),
        ({"status": "ready"}, "write_scope"),
        ({"status": "ready", "scope": []}, "write_scope"),
    ],
)
def test_invalid_stories_are_rejected(tmp_path: Path, kwargs: dict, match: str) -> None:
    _story(tmp_path, "US-001", **kwargs)

    with pytest.raises(DevPlaneError, match=match):
        load_backlog(tmp_path)


def test_backlog_status_may_omit_the_write_scope(tmp_path: Path) -> None:
    _story(tmp_path, "US-001", status="backlog")

    assert load_backlog(tmp_path).stories[0].write_scope == ()


@pytest.mark.parametrize("story_id", ["us 001", "../x", "-bad", ""])
def test_unsafe_ids_are_rejected(tmp_path: Path, story_id: str) -> None:
    _story(tmp_path, story_id or "x", filename="x.md", extra="")
    path = tmp_path / "backlog" / "x.md"
    path.write_text(
        path.read_text().replace("id: x", f"id: '{story_id}'"), encoding="utf-8"
    )

    with pytest.raises(DevPlaneError):
        load_backlog(tmp_path)


def test_filename_must_start_with_the_story_id(tmp_path: Path) -> None:
    _story(tmp_path, "US-001", filename="other-name.md")

    with pytest.raises(DevPlaneError, match="file name"):
        load_backlog(tmp_path)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    _story(tmp_path, "US-001")
    _story(tmp_path, "US-001", filename="US-001-copy.md")

    with pytest.raises(DevPlaneError, match="duplicate"):
        load_backlog(tmp_path)


def test_dependency_cycles_are_rejected(tmp_path: Path) -> None:
    _story(tmp_path, "US-001", depends_on=["US-002"])
    _story(tmp_path, "US-002", depends_on=["US-003"])
    _story(tmp_path, "US-003", depends_on=["US-001"])

    with pytest.raises(DevPlaneError, match="cycle"):
        load_backlog(tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        "no frontmatter\n",
        "---\nid: US-001\n",
        "---\n: : :\n---\n",
        "---\n- a\n- b\n---\n",
    ],
)
def test_malformed_frontmatter_is_rejected(tmp_path: Path, content: str) -> None:
    (tmp_path / "backlog").mkdir()
    (tmp_path / "backlog" / "US-001.md").write_text(content, encoding="utf-8")

    with pytest.raises(DevPlaneError):
        load_backlog(tmp_path)


def test_oversized_story_files_are_rejected(tmp_path: Path) -> None:
    _story(tmp_path, "US-001", body="x" * 300_000)

    with pytest.raises(DevPlaneError, match="too large"):
        load_backlog(tmp_path)


def test_symlinked_story_files_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\nid: US-001\ntitle: T\nstatus: backlog\n---\n", encoding="utf-8"
    )
    (tmp_path / "backlog").mkdir()
    os.symlink(outside, tmp_path / "backlog" / "US-001.md")

    with pytest.raises(DevPlaneError, match="symlink"):
        load_backlog(tmp_path)


def test_symlinked_backlog_directory_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    os.symlink(real, tmp_path / "backlog")

    with pytest.raises(DevPlaneError, match="symlink"):
        load_backlog(tmp_path)


# ---------------------------------------------------------------------------
# per-story validation
# ---------------------------------------------------------------------------


def test_validation_commands_are_parsed_and_default_to_none(tmp_path: Path) -> None:
    _story(tmp_path, "US-001", extra="validation: ['python3 -m unittest', 'test -f docs/HELP.md']")
    _story(tmp_path, "US-002")

    backlog = load_backlog(tmp_path)

    assert backlog.get("US-001").validation == ("python3 -m unittest", "test -f docs/HELP.md")
    assert backlog.get("US-002").validation == ()


@pytest.mark.parametrize(
    "extra",
    [
        "validation: python3 -m unittest",
        "validation: ['']",
        "validation: ['   ']",
        "validation: [3]",
        "validation: ['pytest && echo done']",
        "validation: ['pytest | tee log']",
        "validation: [\"unterminated 'quote\"]",
    ],
)
def test_invalid_validation_commands_are_rejected(tmp_path: Path, extra: str) -> None:
    _story(tmp_path, "US-001", extra=extra)

    with pytest.raises(DevPlaneError, match="validation") as excinfo:
        load_backlog(tmp_path)

    assert "unknown" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------


def test_ready_stories_need_ready_status_and_finished_dependencies(
    tmp_path: Path,
) -> None:
    _story(tmp_path, "US-001", status="done", scope=["a.py"])
    _story(tmp_path, "US-002", status="ready", scope=["b.py"], depends_on=["US-001"])
    _story(tmp_path, "US-003", status="ready", scope=["c.py"], depends_on=["US-004"])
    _story(tmp_path, "US-004", status="in_progress", scope=["d.py"])
    _story(tmp_path, "US-005", status="backlog")

    assert [story.id for story in ready_stories(load_backlog(tmp_path))] == ["US-002"]


def test_ready_stories_are_ordered_by_priority_then_id(tmp_path: Path) -> None:
    _story(tmp_path, "US-001", status="ready", scope=["a.py"], priority="low")
    _story(tmp_path, "US-002", status="ready", scope=["b.py"], priority="urgent")
    _story(tmp_path, "US-003", status="ready", scope=["c.py"], priority="normal")
    _story(tmp_path, "US-004", status="ready", scope=["d.py"], priority="urgent")

    ordered = [story.id for story in ready_stories(load_backlog(tmp_path))]

    assert ordered == ["US-002", "US-004", "US-003", "US-001"]


# ---------------------------------------------------------------------------
# parallel planning
# ---------------------------------------------------------------------------


def test_disjoint_stories_share_the_first_batch(tmp_path: Path) -> None:
    _story(tmp_path, "US-001", status="ready", scope=["src/auth/**"])
    _story(tmp_path, "US-002", status="ready", scope=["src/billing/**"])

    plan = plan_batches(load_backlog(tmp_path), max_parallel=4)

    assert [[story.id for story in batch] for batch in plan.batches] == [
        ["US-001", "US-002"]
    ]
    assert plan.deferred == {}


def test_overlapping_stories_are_serialised_with_a_reason(tmp_path: Path) -> None:
    _story(tmp_path, "US-001", status="ready", scope=["src/auth/**"], priority="high")
    _story(tmp_path, "US-002", status="ready", scope=["src/auth/models.py"])
    _story(tmp_path, "US-003", status="ready", scope=["docs/**"])

    plan = plan_batches(load_backlog(tmp_path), max_parallel=4)

    assert [[story.id for story in batch] for batch in plan.batches] == [
        ["US-001", "US-003"],
        ["US-002"],
    ]
    assert "US-001" in plan.deferred["US-002"]
    assert "src/auth" in plan.deferred["US-002"]
    assert "US-003" not in plan.deferred


def test_max_parallel_limits_the_batch_size(tmp_path: Path) -> None:
    for index in range(1, 6):
        _story(tmp_path, f"US-00{index}", status="ready", scope=[f"pkg{index}/**"])

    plan = plan_batches(load_backlog(tmp_path), max_parallel=2)

    assert [[story.id for story in batch] for batch in plan.batches] == [
        ["US-001", "US-002"],
        ["US-003", "US-004"],
        ["US-005"],
    ]
    assert "parallel limit" in plan.deferred["US-003"]


def test_stories_waiting_on_dependencies_are_not_planned(tmp_path: Path) -> None:
    _story(tmp_path, "US-001", status="in_progress", scope=["a/**"])
    _story(tmp_path, "US-002", status="ready", scope=["b/**"], depends_on=["US-001"])

    plan = plan_batches(load_backlog(tmp_path), max_parallel=2)

    assert plan.batches == ()
    assert plan.waiting == {"US-002": ["US-001"]}


def test_in_progress_scopes_block_overlapping_ready_stories(tmp_path: Path) -> None:
    _story(tmp_path, "US-001", status="in_progress", scope=["src/auth/**"])
    _story(tmp_path, "US-002", status="ready", scope=["src/auth/models.py"])
    _story(tmp_path, "US-003", status="ready", scope=["docs/**"])

    plan = plan_batches(load_backlog(tmp_path), max_parallel=4)

    assert [[story.id for story in batch] for batch in plan.batches] == [["US-003"]]
    assert "US-001" in plan.deferred["US-002"]
    assert "in progress" in plan.deferred["US-002"]


def test_max_parallel_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(DevPlaneError, match="max_parallel"):
        plan_batches(load_backlog(tmp_path), max_parallel=0)


# ---------------------------------------------------------------------------
# status transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("backlog", "ready"),
        ("ready", "in_progress"),
        ("in_progress", "review"),
        ("review", "done"),
        ("review", "in_progress"),
        ("in_progress", "blocked"),
        ("blocked", "ready"),
    ],
)
def test_valid_transitions(old: str, new: str) -> None:
    check_transition(old, new)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("backlog", "done"),
        ("backlog", "in_progress"),
        ("ready", "done"),
        ("done", "ready"),
        ("done", "backlog"),
        ("review", "backlog"),
        ("ready", "ready"),
        ("ready", "nonsense"),
    ],
)
def test_invalid_transitions(old: str, new: str) -> None:
    with pytest.raises(DevPlaneError):
        check_transition(old, new)


def test_set_status_rewrites_only_the_status_line(tmp_path: Path) -> None:
    path = _story(
        tmp_path,
        "US-001",
        status="backlog",
        scope=["src/a.py"],
        body="# Heading\n\nstatus: not frontmatter\n",
    )
    other = _story(tmp_path, "US-002")
    before_other = other.read_text()
    original = path.read_text()

    set_status(tmp_path, "US-001", "ready")

    updated = path.read_text()
    assert updated == original.replace("status: backlog\n", "status: ready\n", 1)
    assert "status: not frontmatter" in updated
    assert other.read_text() == before_other
    assert load_backlog(tmp_path).get("US-001").status == "ready"


def test_set_status_rejects_invalid_transitions_and_unknown_stories(
    tmp_path: Path,
) -> None:
    path = _story(tmp_path, "US-001", status="backlog")
    before = path.read_text()

    with pytest.raises(DevPlaneError, match="transition"):
        set_status(tmp_path, "US-001", "done")
    with pytest.raises(DevPlaneError, match="unknown story"):
        set_status(tmp_path, "US-404", "ready")

    assert path.read_text() == before


def test_set_status_refuses_to_leave_the_backlog_invalid(tmp_path: Path) -> None:
    path = _story(tmp_path, "US-001", status="backlog")  # no write_scope
    before = path.read_text()

    with pytest.raises(DevPlaneError, match="write_scope"):
        set_status(tmp_path, "US-001", "ready")

    assert path.read_text() == before


# ---------------------------------------------------------------------------
# linking a story to a feature run
# ---------------------------------------------------------------------------


def test_set_feature_adds_the_key_and_leaves_everything_else_alone(tmp_path: Path) -> None:
    from devplane.backlog import set_feature

    path = _story(tmp_path, "US-001", status="ready", scope=["src/a.py"], body="Body with feature: not yaml\n")
    original = path.read_text()

    story = set_feature(tmp_path, "US-001", "spec-US-001")

    assert story.feature == "spec-US-001"
    updated = path.read_text()
    assert updated.replace("feature: spec-US-001\n", "", 1) == original
    assert "Body with feature: not yaml" in updated
    assert load_backlog(tmp_path).get("US-001").feature == "spec-US-001"


def test_set_feature_replaces_an_existing_value(tmp_path: Path) -> None:
    from devplane.backlog import set_feature

    path = _story(tmp_path, "US-001", extra="feature: old-run")

    set_feature(tmp_path, "US-001", "new-run")

    assert path.read_text().count("feature:") == 1
    assert load_backlog(tmp_path).get("US-001").feature == "new-run"


@pytest.mark.parametrize("value", ["", "bad feature!", "../x", "x" * 200])
def test_set_feature_rejects_invalid_values_and_leaves_the_file_alone(tmp_path: Path, value: str) -> None:
    from devplane.backlog import set_feature

    path = _story(tmp_path, "US-001")
    before = path.read_text()

    with pytest.raises(DevPlaneError, match="feature"):
        set_feature(tmp_path, "US-001", value)

    assert path.read_text() == before


def test_set_feature_rejects_unknown_stories(tmp_path: Path) -> None:
    from devplane.backlog import set_feature

    with pytest.raises(DevPlaneError, match="unknown story"):
        set_feature(tmp_path, "US-404", "run-1")
