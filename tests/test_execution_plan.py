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


def test_build_execution_plan_rejects_overlapping_parallel_scopes() -> None:
    phases = parse_tasks_markdown(
        "## Phase 3: User Story 1\n"
        "- [ ] T001 [US1] Modify service in src/service.py\n"
        "## Phase 4: User Story 2\n"
        "- [ ] T002 [US2] Also modify service in src/service.py\n"
    )

    with pytest.raises(DevPlaneError, match="overlapping write scope"):
        build_execution_plan(
            phases,
            run_id="run-123",
            source_tasks="specs/001/tasks.md",
            manifest_digest="sha256:manifest",
            base_commit="abcdef0",
            validation_commands=["pytest"],
        )


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
