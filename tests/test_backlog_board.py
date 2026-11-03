"""Tests for story creation and the board view."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from devplane.backlog import create_story, load_backlog, next_story_id
from devplane.backlog_board import build_board, render_markdown, render_text
from devplane.core import DevPlaneError


def _story(
    project: Path,
    story_id: str,
    *,
    status: str = "backlog",
    scope: str | None = None,
    priority: str = "normal",
    title: str | None = None,
    depends_on: str = "[]",
) -> None:
    directory = project / "backlog"
    directory.mkdir(exist_ok=True)
    scope_line = f"write_scope: ['{scope}']\n" if scope else ""
    (directory / f"{story_id}.md").write_text(
        f"---\nid: {story_id}\ntitle: {title or 'Story ' + story_id}\nstatus: {status}\n"
        f"priority: {priority}\ndepends_on: {depends_on}\n{scope_line}---\nBody.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_writes_a_valid_story_with_a_generated_id_and_slug(tmp_path: Path) -> None:
    story = create_story(
        tmp_path,
        title="Función de suma",
        priority="high",
        write_scope=["calc/add.py", "tests/test_add.py"],
        validation=["python3 -m unittest discover -s tests"],
        feature="feature-1",
        body="Accept: add(1, 2) == 3",
    )

    assert story.id == "US-001"
    assert story.path == tmp_path / "backlog" / "US-001-funcion-de-suma.md"
    loaded = load_backlog(tmp_path).get("US-001")
    assert loaded.title == "Función de suma"
    assert (loaded.status, loaded.priority) == ("backlog", "high")
    assert loaded.write_scope == ("calc/add.py", "tests/test_add.py")
    assert loaded.validation == ("python3 -m unittest discover -s tests",)
    assert loaded.feature == "feature-1"
    assert loaded.body.strip() == "Accept: add(1, 2) == 3"


def test_ids_continue_after_the_highest_existing_number(tmp_path: Path) -> None:
    _story(tmp_path, "US-001")
    _story(tmp_path, "US-007")
    _story(tmp_path, "OTHER-9")

    assert next_story_id(load_backlog(tmp_path)) == "US-008"
    assert next_story_id(load_backlog(tmp_path), prefix="OTHER") == "OTHER-010"
    assert next_story_id(load_backlog(tmp_path), prefix="NEW") == "NEW-001"
    assert create_story(tmp_path, title="Next one").id == "US-008"


def test_an_explicit_id_is_honoured_and_must_be_unique(tmp_path: Path) -> None:
    created = create_story(tmp_path, title="Custom", story_id="API-12")

    assert created.id == "API-12"
    with pytest.raises(DevPlaneError, match="duplicate"):
        create_story(tmp_path, title="Again", story_id="API-12")


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"title": ""}, "title"),
        ({"title": "   "}, "title"),
        ({"title": "two\nlines"}, "title"),
        ({"title": "T", "status": "done"}, "status"),
        ({"title": "T", "status": "ready"}, "write_scope"),
        ({"title": "T", "priority": "asap"}, "priority"),
        ({"title": "T", "write_scope": ["src/[ab]"]}, "scope"),
        ({"title": "T", "write_scope": ["specs/**"]}, "governed"),
        ({"title": "T", "validation": ["pytest && echo done"]}, "validation"),
        ({"title": "T", "depends_on": ["US-404"]}, "unknown story"),
        ({"title": "T", "story_id": "bad id"}, "id"),
        ({"title": "T", "feature": "bad feature!"}, "feature"),
    ],
)
def test_invalid_stories_are_rejected_and_nothing_is_written(
    tmp_path: Path, kwargs: dict, match: str
) -> None:
    with pytest.raises(DevPlaneError, match=match):
        create_story(tmp_path, **kwargs)

    assert not (tmp_path / "backlog").exists() or list((tmp_path / "backlog").iterdir()) == []


def test_a_ready_story_with_a_scope_can_be_created_and_depend_on_existing_ones(tmp_path: Path) -> None:
    create_story(tmp_path, title="First")

    second = create_story(tmp_path, title="Second", status="ready", write_scope=["src/a.py"], depends_on=["US-001"])

    assert second.status == "ready"
    assert second.depends_on == ("US-001",)


@pytest.mark.parametrize(
    ("title", "slug"),
    [("Hello, World!", "hello-world"), ("¡¡¡", "story"), ("A" * 80, "a" * 40), ("  --x--  ", "x")],
)
def test_slugs_are_safe_ascii(tmp_path: Path, title: str, slug: str) -> None:
    assert create_story(tmp_path, title=title).path.name == f"US-001-{slug}.md"


def test_create_refuses_a_symlinked_backlog_directory(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    os.symlink(real, tmp_path / "backlog")

    with pytest.raises(DevPlaneError, match="symlink"):
        create_story(tmp_path, title="T")

    assert list(real.iterdir()) == []


def test_create_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    (tmp_path / "backlog").mkdir()
    target = tmp_path / "backlog" / "US-001-taken.md"
    target.write_text("not a story, but there first\n", encoding="utf-8")
    # The stray file is unparseable, so loading the backlog fails before anything is written.
    with pytest.raises(DevPlaneError):
        create_story(tmp_path, title="Taken")

    assert target.read_text(encoding="utf-8") == "not a story, but there first\n"


# ---------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------


def _board_backlog(tmp_path: Path):
    _story(tmp_path, "US-001", status="ready", scope="src/auth/**", priority="high", title="Auth")
    _story(tmp_path, "US-002", status="ready", scope="docs/**", title="Docs")
    _story(tmp_path, "US-003", status="ready", scope="src/auth/models.py", title="Models")
    _story(tmp_path, "US-004", status="in_progress", scope="lib/**", title="Lib")
    _story(tmp_path, "US-005", status="ready", scope="lib/x.py", title="Lib extra")
    _story(
        tmp_path, "US-006", status="ready", scope="web/**", title="Web", priority="urgent", depends_on="[US-004]"
    )
    _story(tmp_path, "US-007", status="backlog", title="Idea")
    _story(tmp_path, "US-008", status="review", scope="rev/**", title="Reviewing")
    _story(tmp_path, "US-009", status="blocked", title="Stuck")
    _story(tmp_path, "US-010", status="done", scope="old/**", title="Finished")
    return load_backlog(tmp_path)


def test_the_board_groups_stories_by_status_in_priority_order(tmp_path: Path) -> None:
    board = build_board(_board_backlog(tmp_path), max_parallel=2)

    columns = {name: [card["id"] for card in cards] for name, cards in board["columns"].items()}
    assert list(board["columns"]) == ["backlog", "ready", "in_progress", "review", "blocked", "done"]
    assert columns["ready"] == ["US-006", "US-001", "US-002", "US-003", "US-005"]
    assert columns["backlog"] == ["US-007"]
    assert columns["in_progress"] == ["US-004"]
    assert columns["done"] == ["US-010"]


def test_ready_cards_say_what_happens_to_them(tmp_path: Path) -> None:
    board = build_board(_board_backlog(tmp_path), max_parallel=2)
    notes = {card["id"]: card["note"] for card in board["columns"]["ready"]}

    assert notes["US-001"] == "runs next"
    assert notes["US-002"] == "runs next"
    assert notes["US-003"].startswith("wave 2: overlaps US-001")
    assert notes["US-005"].startswith("deferred: overlaps US-004 (in progress)")
    assert notes["US-006"] == "waits for US-004"
    assert board["next_batch"] == ["US-001", "US-002"]


def test_cards_for_other_statuses_carry_only_useful_notes(tmp_path: Path) -> None:
    board = build_board(_board_backlog(tmp_path), max_parallel=2)

    assert board["columns"]["backlog"][0]["note"] == "no write_scope yet"
    assert board["columns"]["in_progress"][0]["note"] == ""
    assert board["columns"]["done"][0]["note"] == ""


def test_text_rendering_shows_every_column_with_counts(tmp_path: Path) -> None:
    text = render_text(build_board(_board_backlog(tmp_path), max_parallel=2))

    for header in ("BACKLOG (1)", "READY (5)", "IN PROGRESS (1)", "REVIEW (1)", "BLOCKED (1)", "DONE (1)"):
        assert header in text
    assert "US-001" in text and "high" in text and "— runs next" in text
    assert "next batch: US-001, US-002" in text


def test_markdown_rendering_is_a_table_per_column(tmp_path: Path) -> None:
    markdown = render_markdown(build_board(_board_backlog(tmp_path), max_parallel=2))

    assert "### Ready (5)" in markdown
    assert "| US-001 | high | Auth |" in markdown
    assert "| --- |" in markdown


def test_an_empty_backlog_renders_empty_columns(tmp_path: Path) -> None:
    text = render_text(build_board(load_backlog(tmp_path), max_parallel=2))

    assert "BACKLOG (0)" in text and "DONE (0)" in text
    assert "next batch: none" in text


def test_create_survives_a_file_appearing_right_before_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from devplane import backlog as backlog_module

    (tmp_path / "backlog").mkdir()
    taken = tmp_path / "backlog" / "US-001-race.md"
    taken.write_text("written by someone else first\n", encoding="utf-8")
    # The existing backlog is read before the file exists; simulate that stale view.
    monkeypatch.setattr(backlog_module, "load_backlog", lambda _project: backlog_module.Backlog(()))

    with pytest.raises(DevPlaneError, match="already exists"):
        create_story(tmp_path, title="Race")

    assert taken.read_text(encoding="utf-8") == "written by someone else first\n"
