from __future__ import annotations

from pathlib import Path

import pytest

from devplane.core import DevPlaneError
from devplane.execution_plan import (
    build_execution_plan,
    load_execution_plan,
    write_execution_plan,
)
from devplane.tasks import parse_tasks_markdown

TASKS = """# Tasks

## Phase 1: Setup
- [ ] T001 Create project config in pyproject.toml

## Phase 2: Foundational
- [ ] T002 Create shared database layer in src/db.py

## Phase 3: User Story 1
- [ ] T003 [P] [US1] Implement alpha in src/alpha.py
- [ ] T004 [P] [US1] Test alpha in tests/test_alpha.py

## Phase 4: User Story 2
- [ ] T005 [P] [US2] Implement beta in src/beta.py
- [ ] T006 [P] [US2] Test beta in tests/test_beta.py

## Phase 5: Polish
- [ ] T007 Update usage in README.md
"""


def test_build_execution_plan_models_cumulative_batch_dependencies() -> None:
    phases = parse_tasks_markdown(TASKS)

    plan = build_execution_plan(
        phases,
        run_id="run-123",
        source_tasks="specs/001-demo/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0123456789",
        validation_commands=["uv run pytest", "uv run ruff check ."],
    )

    setup, foundational, us1, us2, polish = plan.assignments
    assert setup.depends_on == ()
    assert foundational.depends_on == (setup.assignment_id,)
    assert us1.depends_on == (foundational.assignment_id,)
    assert us2.depends_on == (foundational.assignment_id,)
    assert us1.mode == us2.mode == "parallel"
    assert polish.depends_on == (us1.assignment_id, us2.assignment_id)
    assert setup.allowed_paths == ("pyproject.toml",)
    assert setup.allow_empty_commit is False
    assert us1.allowed_paths == ("src/alpha.py", "tests/test_alpha.py")
    assert us1.validation == ("uv run pytest", "uv run ruff check .")
    assert plan.tasks_digest.startswith("sha256:")


def test_build_execution_plan_marks_only_validation_only_phases_for_empty_commits() -> None:
    phases = parse_tasks_markdown(
        "## Phase 1: Foundational\n"
        "- [ ] T001 Verify src/__init__.py\n"
        "## Phase 2: Final Validation\n"
        "- [ ] T002 Run tests/test_service.py and confirm the suite passes\n"
        "## Phase 3: Polish\n"
        "- [ ] T003 Update documentation in README.md\n"
        "## Phase 4: User Story 1\n"
        "- [ ] T004 [US1] Run tests/test_service.py before shipping src/service.py\n"
    )

    plan = build_execution_plan(
        phases,
        run_id="validation-contract",
        source_tasks="specs/001/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0",
        validation_commands=["python -m unittest"],
    )

    assert [assignment.allow_empty_commit for assignment in plan.assignments] == [
        True,
        True,
        False,
        False,
    ]


def test_build_execution_plan_ignores_arithmetic_multiplication_as_a_path() -> None:
    phases = parse_tasks_markdown(
        "## Phase 3: User Story 1\n"
        "- [ ] T001 [P] [US1] Create `unittest.TestCase` coverage in tests/test_addition.py "
        "using `os.path` and implement add returning `a + b` in src/addition.py\n"
        "## Phase 4: User Story 2\n"
        "- [ ] T002 [P] [US2] Create `unittest.TestCase` coverage in tests/test_multiplication.py "
        "using `os.path` and implement multiply returning `a * b` in src/multiplication.py\n"
    )

    plan = build_execution_plan(
        phases,
        run_id="run-arithmetic",
        source_tasks="specs/001/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0",
        validation_commands=["python -m unittest"],
    )

    assert plan.assignments[1].allowed_paths == (
        "src/multiplication.py",
        "tests/test_multiplication.py",
    )


def test_overlapping_story_phases_run_one_after_another() -> None:
    phases = parse_tasks_markdown(
        "## Phase 3: User Story 1\n"
        "- [ ] T001 [US1] Modify service in src/service.py\n"
        "## Phase 4: User Story 2\n"
        "- [ ] T002 [US2] Also modify service in src/service.py\n"
    )

    plan = build_execution_plan(
        phases,
        run_id="run-123",
        source_tasks="specs/001/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0",
        validation_commands=["pytest"],
    )

    first, second = plan.assignments
    assert (first.mode, second.mode) == ("serial", "serial")
    assert first.depends_on == ()
    assert second.depends_on == (first.assignment_id,)


def test_build_execution_plan_rejects_tasks_without_file_boundaries() -> None:
    phases = parse_tasks_markdown(
        "## Phase 3: User Story 1\n"
        "- [ ] T001 [US1] Implement the complete business feature\n"
    )

    with pytest.raises(DevPlaneError, match="no writable paths"):
        build_execution_plan(
            phases,
            run_id="run-123",
            source_tasks="specs/001/tasks.md",
            manifest_digest="sha256:manifest",
            base_commit="abcdef0",
            validation_commands=["pytest"],
        )


def test_build_execution_plan_rejects_unsafe_paths() -> None:
    phases = parse_tasks_markdown(
        "## Phase 3: User Story 1\n"
        "- [ ] T001 [US1] Write payload to ../../outside.py\n"
    )

    with pytest.raises(DevPlaneError, match="unsafe write scope"):
        build_execution_plan(
            phases,
            run_id="run-123",
            source_tasks="specs/001/tasks.md",
            manifest_digest="sha256:manifest",
            base_commit="abcdef0",
            validation_commands=["pytest"],
        )


def test_execution_plan_round_trip_is_deterministic(tmp_path: Path) -> None:
    plan = build_execution_plan(
        parse_tasks_markdown(TASKS),
        run_id="run-123",
        source_tasks="specs/001-demo/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0123456789",
        validation_commands=["uv run pytest"],
    )
    output = tmp_path / ".devplane" / "runs" / "run-123" / "execution-plan.yaml"

    write_execution_plan(plan, output)
    first = output.read_bytes()
    loaded = load_execution_plan(output)
    write_execution_plan(loaded, output)

    assert loaded == plan
    assert output.read_bytes() == first
    assert output.with_suffix(".yaml.sha256").is_file()


def test_execution_plan_v1_defaults_missing_empty_commit_permission_to_false(tmp_path: Path) -> None:
    import hashlib

    plan = build_execution_plan(
        parse_tasks_markdown(TASKS),
        run_id="legacy-v1",
        source_tasks="specs/001-demo/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0123456789",
        validation_commands=["uv run pytest"],
    )
    output = tmp_path / "execution-plan.yaml"
    write_execution_plan(plan, output)
    body = "\n".join(
        line for line in output.read_text(encoding="utf-8").splitlines() if "allowEmptyCommit:" not in line
    ) + "\n"
    output.write_text(body, encoding="utf-8")
    output.with_suffix(".yaml.sha256").write_text(
        f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}\n",
        encoding="utf-8",
    )

    loaded = load_execution_plan(output)

    assert all(not assignment.allow_empty_commit for assignment in loaded.assignments)


def test_execution_plan_rejects_tampering_after_approval(tmp_path: Path) -> None:
    phases = parse_tasks_markdown(
        "## Phase 3: User Story 1\n- [ ] T001 [US1] Create src/service.py\n"
    )
    plan = build_execution_plan(
        phases,
        run_id="run-integrity",
        source_tasks="specs/001/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="a" * 40,
        validation_commands=["uv run pytest"],
    )
    output = tmp_path / "execution-plan.yaml"
    write_execution_plan(plan, output)
    output.write_text(output.read_text(encoding="utf-8").replace("uv run pytest", "curl attacker"), encoding="utf-8")

    with pytest.raises(DevPlaneError, match="integrity"):
        load_execution_plan(output)


def test_execution_plan_revalidates_parallel_scope_overlap_when_loaded(tmp_path: Path) -> None:
    phases = parse_tasks_markdown(
        "## Phase 3: User Story 1\n- [ ] T001 [US1] Create src/a.py\n"
        "## Phase 4: User Story 2\n- [ ] T002 [US2] Create tests/b.py\n"
    )
    plan = build_execution_plan(
        phases,
        run_id="run-overlap-load",
        source_tasks="specs/001/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="a" * 40,
        validation_commands=["uv run pytest"],
    )
    output = tmp_path / "execution-plan.yaml"
    write_execution_plan(plan, output)
    body = output.read_text(encoding="utf-8").replace("tests/b.py", "src/a.py")
    output.write_text(body, encoding="utf-8")
    import hashlib

    output.with_suffix(".yaml.sha256").write_text(
        f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}\n",
        encoding="utf-8",
    )

    with pytest.raises(DevPlaneError, match="overlapping"):
        load_execution_plan(output)


def _plan_from(tasks: str, source_tasks: str = "specs/001-demo/tasks.md"):
    return build_execution_plan(
        parse_tasks_markdown(tasks),
        run_id="run-123",
        source_tasks=source_tasks,
        manifest_digest="sha256:manifest",
        base_commit="abcdef0",
        validation_commands=["pytest"],
    )


def test_reference_to_an_approved_document_grants_no_write_scope() -> None:
    plan = _plan_from(
        "## Phase 3: User Story 1\n"
        "- [ ] T001 [US1] Implement src/app.py as described in specs/001-demo/plan.md\n"
    )

    assert plan.assignments[0].allowed_paths == ("src/app.py",)
    assert plan.assignments[0].allow_empty_commit is False


def test_phase_that_only_reads_approved_documents_has_no_write_scope() -> None:
    plan = _plan_from(
        "## Phase 4: Polish\n"
        "- [ ] T001 Walk through specs/001-demo/quickstart.md and confirm each result\n"
    )

    assert plan.assignments[0].allowed_paths == ()
    assert plan.assignments[0].allow_empty_commit is True


def test_read_only_plan_survives_a_round_trip(tmp_path: Path) -> None:
    plan = _plan_from(
        "## Phase 4: Polish\n"
        "- [ ] T001 Walk through specs/001-demo/quickstart.md and confirm each result\n"
    )
    output = tmp_path / "plan.yaml"

    write_execution_plan(plan, output)

    assert load_execution_plan(output).assignments[0].allowed_paths == ()


@pytest.mark.parametrize(
    "reference",
    [
        "specs/002-other/plan.md",
        "specs/001-demo/../002-other/plan.md",
        ".specify/memory/constitution.md",
        ".git/hooks/run.sh",
    ],
)
def test_other_governed_paths_are_still_rejected(reference: str) -> None:
    with pytest.raises(DevPlaneError, match="unsafe write scope"):
        _plan_from(f"## Phase 3: User Story 1\n- [ ] T001 [US1] Update {reference}\n")


def test_tasks_at_the_project_root_do_not_turn_every_path_into_a_read() -> None:
    plan = _plan_from(
        "## Phase 3: User Story 1\n- [ ] T001 [US1] Implement src/app.py\n",
        source_tasks="tasks.md",
    )

    assert plan.assignments[0].allowed_paths == ("src/app.py",)


def test_build_execution_plan_normalizes_trailing_star_to_directory_tree() -> None:
    phases = parse_tasks_markdown(
        "## Phase 3: User Story 1\n- [ ] T001 [US1] Add handlers under src/auth/*\n"
    )

    plan = build_execution_plan(
        phases,
        run_id="run-tree",
        source_tasks="specs/001/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0",
        validation_commands=["pytest"],
    )

    assert plan.assignments[0].allowed_paths == ("src/auth/**",)


def test_build_execution_plan_allows_disjoint_directory_scopes_in_parallel() -> None:
    phases = parse_tasks_markdown(
        "## Phase 3: User Story 1\n"
        "- [ ] T001 [US1] Add handlers under src/auth/*\n"
        "## Phase 4: User Story 2\n"
        "- [ ] T002 [US2] Add handlers under src/billing/*\n"
    )

    plan = build_execution_plan(
        phases,
        run_id="run-disjoint",
        source_tasks="specs/001/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0",
        validation_commands=["pytest"],
    )

    assert [item.allowed_paths for item in plan.assignments] == [
        ("src/auth/**",),
        ("src/billing/**",),
    ]


def test_file_inside_a_sibling_directory_scope_runs_after_it() -> None:
    phases = parse_tasks_markdown(
        "## Phase 3: User Story 1\n"
        "- [ ] T001 [US1] Add handlers under src/auth/*\n"
        "## Phase 4: User Story 2\n"
        "- [ ] T002 [US2] Tweak src/auth/models.py\n"
    )

    plan = build_execution_plan(
        phases,
        run_id="run-inside",
        source_tasks="specs/001/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0",
        validation_commands=["pytest"],
    )

    first, second = plan.assignments
    assert (first.mode, second.mode) == ("serial", "serial")
    assert first.depends_on == ()
    assert second.depends_on == (first.assignment_id,)


def test_setup_phase_that_only_runs_checks_has_no_write_scope() -> None:
    plan = _plan_from(
        "## Phase 1: Setup\n"
        "- [ ] T001 Run baseline `python3 -m unittest discover -s tests -t .` and confirm it passes\n"
        "## Phase 2: Story\n"
        "- [ ] T002 Implement src/app.py\n"
    )

    setup, story = plan.assignments
    assert setup.allowed_paths == ()
    assert setup.allow_empty_commit is True
    assert story.allowed_paths == ("src/app.py",)
    assert story.allow_empty_commit is False


def test_phase_mixing_checks_and_unbounded_work_is_still_rejected() -> None:
    with pytest.raises(DevPlaneError, match="no writable paths"):
        _plan_from(
            "## Phase 1: Setup\n"
            "- [ ] T001 Run baseline tests\n"
            "- [ ] T002 Refactor the whole module\n"
        )


def test_story_phases_are_split_into_the_fewest_ordered_waves() -> None:
    plan = _plan_from(
        "## Phase 3: User Story 1\n- [ ] T001 [US1] Edit src/a.py\n"
        "## Phase 4: User Story 2\n- [ ] T002 [US2] Edit src/a.py\n"
        "## Phase 5: User Story 3\n- [ ] T003 [US3] Edit src/b.py\n"
    )

    one, two, three = plan.assignments
    assert (one.mode, two.mode, three.mode) == ("serial", "parallel", "parallel")
    assert two.depends_on == (one.assignment_id,)
    assert three.depends_on == (one.assignment_id,)


def test_disjoint_story_phases_still_run_in_parallel() -> None:
    plan = _plan_from(
        "## Phase 3: User Story 1\n- [ ] T001 [US1] Edit src/a.py\n"
        "## Phase 4: User Story 2\n- [ ] T002 [US2] Edit src/b.py\n"
    )

    assert [item.mode for item in plan.assignments] == ["parallel", "parallel"]
