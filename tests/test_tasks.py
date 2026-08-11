"""Tests for the GitHub Spec Kit tasks.md parser and execution batch builder.

The contract focuses on a deterministic parser for the canonical Spec Kit
``tasks.md`` shape (``## <phase>`` headings plus ``- [ ] T001 [P]? [US1]?
description`` checklist lines) and on a scheduler that splits the resulting
phases into serial and parallel batches.
"""

from __future__ import annotations

import pytest

from devplane.tasks import (
    SpecTask,
    TaskPhase,
    build_execution_batches,
    parse_tasks_markdown,
)

# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


def test_parse_returns_phases_with_tasks() -> None:
    text = (
        "## Phase 1: Setup\n"
        "\n"
        "- [ ] T001 create project skeleton\n"
        "- [ ] T002 [P] bootstrap tooling\n"
        "\n"
        "## Phase 2: Foundational\n"
        "\n"
        "- [ ] T003 shared library\n"
        "\n"
        "## Phase 3: User Story 1\n"
        "\n"
        "- [ ] T004 [P] [US1] endpoint handler\n"
        "- [ ] T005 [US1] tests for endpoint\n"
        "\n"
        "## Phase 4: Polish\n"
        "\n"
        "- [ ] T006 docs polish\n"
    )

    phases = parse_tasks_markdown(text)

    assert [phase.name for phase in phases] == [
        "Phase 1: Setup",
        "Phase 2: Foundational",
        "Phase 3: User Story 1",
        "Phase 4: Polish",
    ]
    assert phases[0].tasks == [
        SpecTask(task_id="T001", description="create project skeleton", parallel=False, story=None, phase="Phase 1: Setup"),
        SpecTask(task_id="T002", description="bootstrap tooling", parallel=True, story=None, phase="Phase 1: Setup"),
    ]
    assert phases[1].tasks == [
        SpecTask(task_id="T003", description="shared library", parallel=False, story=None, phase="Phase 2: Foundational"),
    ]
    assert phases[2].tasks == [
        SpecTask(task_id="T004", description="endpoint handler", parallel=True, story="US1", phase="Phase 3: User Story 1"),
        SpecTask(task_id="T005", description="tests for endpoint", parallel=False, story="US1", phase="Phase 3: User Story 1"),
    ]
    assert phases[3].tasks == [
        SpecTask(task_id="T006", description="docs polish", parallel=False, story=None, phase="Phase 4: Polish"),
    ]


def test_parse_tolerates_inline_text_between_phases() -> None:
    text = (
        "# Tasks\n"
        "\n"
        "Some prose before any phase.\n"
        "\n"
        "## Phase 1: Setup\n"
        "\n"
        "- [ ] T001 only task\n"
    )

    phases = parse_tasks_markdown(text)

    assert phases == [
        TaskPhase(
            name="Phase 1: Setup",
            tasks=[
                SpecTask(task_id="T001", description="only task", parallel=False, story=None, phase="Phase 1: Setup"),
            ],
        )
    ]


def test_parse_phase_without_heading_text_is_named_blank() -> None:
    text = "## Setup\n\n- [ ] T001 a task\n"

    phases = parse_tasks_markdown(text)

    assert len(phases) == 1
    assert phases[0].name == "Setup"
    assert phases[0].tasks[0].task_id == "T001"


def test_parse_ignores_checked_tasks() -> None:
    text = (
        "## Phase 1: Setup\n"
        "\n"
        "- [x] T001 already done\n"
        "- [ ] T002 pending\n"
    )

    phases = parse_tasks_markdown(text)

    assert [task.task_id for task in phases[0].tasks] == ["T002"]


def test_parse_preserves_task_order_within_phase() -> None:
    text = (
        "## Phase 1: Setup\n"
        "\n"
        "- [ ] T003 third\n"
        "- [ ] T001 first\n"
        "- [ ] T002 second\n"
    )

    phases = parse_tasks_markdown(text)

    assert [task.task_id for task in phases[0].tasks] == ["T003", "T001", "T002"]


def test_parse_accepts_uppercase_parallel_marker() -> None:
    text = "## Phase 1: Setup\n\n- [ ] T001 [P] parallel\n"

    phases = parse_tasks_markdown(text)

    assert phases[0].tasks[0].parallel is True


def test_parse_rejects_duplicate_task_ids_across_phases() -> None:
    text = (
        "## Phase 1: Setup\n"
        "\n"
        "- [ ] T001 setup task\n"
        "\n"
        "## Phase 2: Foundational\n"
        "\n"
        "- [ ] T001 duplicated task\n"
    )

    with pytest.raises(ValueError, match="duplicate task id: T001"):
        parse_tasks_markdown(text)


def test_parse_rejects_duplicate_task_ids_within_phase() -> None:
    text = (
        "## Phase 1: Setup\n"
        "\n"
        "- [ ] T001 first\n"
        "- [ ] T001 second\n"
    )

    with pytest.raises(ValueError, match="duplicate task id: T001"):
        parse_tasks_markdown(text)


def test_parse_rejects_lines_outside_any_phase() -> None:
    text = (
        "- [ ] T001 orphan task\n"
        "\n"
        "## Phase 1: Setup\n"
        "\n"
        "- [ ] T002 valid task\n"
    )

    with pytest.raises(ValueError, match="no phase context"):
        parse_tasks_markdown(text)


def test_parse_rejects_malformed_checkbox_line() -> None:
    text = (
        "## Phase 1: Setup\n"
        "\n"
        "- [ ] not-a-task-id description\n"
    )

    with pytest.raises(ValueError, match="invalid task line"):
        parse_tasks_markdown(text)


def test_parse_rejects_missing_task_id() -> None:
    text = (
        "## Phase 1: Setup\n"
        "\n"
        "- [ ] [P] description without id\n"
    )

    with pytest.raises(ValueError, match="invalid task line"):
        parse_tasks_markdown(text)


def test_parse_errors_when_no_tasks_present() -> None:
    text = (
        "# Heading\n"
        "\n"
        "Just prose, no checklist at all.\n"
        "\n"
        "## Phase 1: Setup\n"
        "\n"
        "More prose inside the phase.\n"
    )

    with pytest.raises(ValueError, match="no tasks"):
        parse_tasks_markdown(text)


def test_parse_errors_on_empty_input() -> None:
    with pytest.raises(ValueError, match="no tasks"):
        parse_tasks_markdown("")


def test_parse_handles_phase_with_only_completed_tasks() -> None:
    text = (
        "## Phase 1: Setup\n"
        "\n"
        "- [x] T001 done\n"
        "\n"
        "## Phase 2: Foundational\n"
        "\n"
        "- [ ] T002 pending\n"
    )

    phases = parse_tasks_markdown(text)

    assert [phase.name for phase in phases] == ["Phase 2: Foundational"]
    assert [task.task_id for task in phases[0].tasks] == ["T002"]


def test_parse_extracts_multiple_user_stories() -> None:
    text = (
        "## Phase 1: User Story 1\n"
        "\n"
        "- [ ] T001 [US1] story one\n"
        "\n"
        "## Phase 2: User Story 2\n"
        "\n"
        "- [ ] T002 [US2] story two\n"
    )

    phases = parse_tasks_markdown(text)

    assert phases[0].tasks[0].story == "US1"
    assert phases[1].tasks[0].story == "US2"


def test_parse_ignores_non_phase_h2_headings() -> None:
    text = (
        "## Tasks\n"
        "\n"
        "intro\n"
        "\n"
        "## Phase 1: Setup\n"
        "\n"
        "- [ ] T001 real task\n"
    )

    phases = parse_tasks_markdown(text)

    # "## Tasks" is treated as a phase heading by the parser (it is an H2),
    # but it carries no tasks so it does not appear in the output.
    assert [phase.name for phase in phases] == ["Phase 1: Setup"]


# ---------------------------------------------------------------------------
# Scheduler tests
# ---------------------------------------------------------------------------


def _phases_fixture() -> list[TaskPhase]:
    return parse_tasks_markdown(
        "## Phase 1: Setup\n"
        "\n"
        "- [ ] T001 setup\n"
        "- [ ] T002 [P] setup parallel\n"
        "\n"
        "## Phase 2: Foundational\n"
        "\n"
        "- [ ] T003 foundational\n"
        "\n"
        "## Phase 3: User Story 1\n"
        "\n"
        "- [ ] T004 [P] [US1] us1 task a\n"
        "- [ ] T005 [US1] us1 task b\n"
        "\n"
        "## Phase 4: User Story 2\n"
        "\n"
        "- [ ] T006 [P] [US2] us2 task a\n"
        "\n"
        "## Phase 5: Polish\n"
        "\n"
        "- [ ] T007 polish\n"
    )


def test_batches_split_serial_and_user_story_phases() -> None:
    batches = build_execution_batches(_phases_fixture())

    # Setup, Foundational, Polish run alone. US1 and US2 are contiguous
    # user story phases and therefore share a parallel batch.
    assert len(batches) == 4
    assert [phase.name for phase in batches[0]] == ["Phase 1: Setup"]
    assert [phase.name for phase in batches[1]] == ["Phase 2: Foundational"]
    assert [phase.name for phase in batches[2]] == [
        "Phase 3: User Story 1",
        "Phase 4: User Story 2",
    ]
    assert [phase.name for phase in batches[3]] == ["Phase 5: Polish"]


def test_batches_keep_serial_phases_each_in_own_batch() -> None:
    batches = build_execution_batches(_phases_fixture())

    # The two leading serial phases must NOT collapse together.
    assert batches[0] != batches[1]
    # The closing Polish phase must NOT collapse with the User Story batch.
    assert batches[-1] != batches[-2]


def test_batches_does_not_merge_user_story_with_polish() -> None:
    batches = build_execution_batches(_phases_fixture())

    last_batch = batches[-1]
    assert last_batch == [batches[-1][0]]
    assert last_batch[0].name == "Phase 5: Polish"


def test_batches_groups_consecutive_user_story_phases_only() -> None:
    text = (
        "## Phase 1: User Story 1\n"
        "\n"
        "- [ ] T001 [US1] a\n"
        "\n"
        "## Phase 2: Foundational\n"
        "\n"
        "- [ ] T002 foundational\n"
        "\n"
        "## Phase 3: User Story 2\n"
        "\n"
        "- [ ] T003 [US2] b\n"
    )
    phases = parse_tasks_markdown(text)
    batches = build_execution_batches(phases)

    # Setup phases (none here) -> US1 alone -> Foundational alone -> US2 alone.
    assert [phase.name for phase in batches[0]] == ["Phase 1: User Story 1"]
    assert [phase.name for phase in batches[1]] == ["Phase 2: Foundational"]
    assert [phase.name for phase in batches[2]] == ["Phase 3: User Story 2"]


def test_batches_with_no_user_story_phases_serializes_everything() -> None:
    phases = parse_tasks_markdown(
        "## Phase 1: Setup\n"
        "\n"
        "- [ ] T001 a\n"
        "\n"
        "## Phase 2: Foundational\n"
        "\n"
        "- [ ] T002 b\n"
        "\n"
        "## Phase 3: Polish\n"
        "\n"
        "- [ ] T003 c\n"
    )
    batches = build_execution_batches(phases)

    assert [phase.name for phase in batches[0]] == ["Phase 1: Setup"]
    assert [phase.name for phase in batches[1]] == ["Phase 2: Foundational"]
    assert [phase.name for phase in batches[2]] == ["Phase 3: Polish"]


def test_batches_groups_three_user_story_phases() -> None:
    phases = parse_tasks_markdown(
        "## Phase 1: User Story 1\n"
        "\n"
        "- [ ] T001 [US1] a\n"
        "\n"
        "## Phase 2: User Story 2\n"
        "\n"
        "- [ ] T002 [US2] b\n"
        "\n"
        "## Phase 3: User Story 3\n"
        "\n"
        "- [ ] T003 [US3] c\n"
    )
    batches = build_execution_batches(phases)

    assert len(batches) == 1
    assert [phase.name for phase in batches[0]] == [
        "Phase 1: User Story 1",
        "Phase 2: User Story 2",
        "Phase 3: User Story 3",
    ]


def test_batches_user_story_phases_isolated_by_serial_phase() -> None:
    phases = parse_tasks_markdown(
        "## Phase 1: Setup\n"
        "\n"
        "- [ ] T001 setup\n"
        "\n"
        "## Phase 2: User Story 1\n"
        "\n"
        "- [ ] T002 [US1] a\n"
        "\n"
        "## Phase 3: Polish\n"
        "\n"
        "- [ ] T003 polish\n"
        "\n"
        "## Phase 4: User Story 2\n"
        "\n"
        "- [ ] T004 [US2] b\n"
    )
    batches = build_execution_batches(phases)

    # Setup -> US1 -> Polish -> US2. Polish interrupts the run so US2 must
    # start a fresh batch rather than being merged with US1.
    assert [phase.name for phase in batches[0]] == ["Phase 1: Setup"]
    assert [phase.name for phase in batches[1]] == ["Phase 2: User Story 1"]
    assert [phase.name for phase in batches[2]] == ["Phase 3: Polish"]
    assert [phase.name for phase in batches[3]] == ["Phase 4: User Story 2"]


def test_batches_returns_empty_for_empty_input() -> None:
    assert build_execution_batches([]) == []