from __future__ import annotations

import re
from importlib import resources

import pytest

from devplane.execution_plan import build_execution_plan
from devplane.parallel import build_phase_prompt
from devplane.pipeline import _assignment_prompt
from devplane.prompts import load_template, render_template
from devplane.tasks import SpecTask, TaskPhase, parse_tasks_markdown

TEMPLATES = (
    "phase_prompt.md",
    "assignment_prompt.md",
    "assignment_final_rules.md",
    "assignment_final_rules_validation_only.md",
)


def _contract_rules(prompt: str) -> list[str]:
    """Return the numbered rules, proving they are numbered without gaps."""
    matches = re.findall(r"(?m)^(\d+)\. (.+)$", prompt)
    assert [int(number) for number, _ in matches] == list(range(1, len(matches) + 1))
    return [rule for _, rule in matches]


def _assert_contains_all(rule: str, *terms: str) -> None:
    missing = [term for term in terms if term not in rule]
    assert not missing, f"{missing} missing from rule: {rule}"


def _assignment_rendering(tasks_markdown: str) -> str:
    plan = build_execution_plan(
        parse_tasks_markdown(tasks_markdown),
        run_id="contract",
        source_tasks="specs/001/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0",
        validation_commands=["python3 -m unittest"],
    )
    return _assignment_prompt(plan.assignments[0], plan)


@pytest.mark.parametrize("name", TEMPLATES)
def test_template_ships_inside_the_package_and_is_english_ascii(name: str) -> None:
    packaged = resources.files("devplane") / "templates" / name

    assert packaged.is_file()
    text = load_template(name)
    assert text == packaged.read_text(encoding="utf-8")
    assert text.isascii()
    assert text.endswith("\n")


def test_render_template_interpolates_every_placeholder() -> None:
    prompt = render_template(
        "phase_prompt.md",
        phase_name="Phase 9: Demo",
        manifest_digest="sha256:digest",
        task_lines="- T900: Demo task",
    )

    assert "Phase 9: Demo" in prompt
    assert "sha256:digest" in prompt
    assert "- T900: Demo task" in prompt
    assert "{" not in prompt


def test_render_template_rejects_a_missing_value() -> None:
    with pytest.raises(KeyError, match="manifest_digest"):
        render_template("phase_prompt.md", phase_name="x", task_lines="y")


def test_braces_in_task_text_are_data_not_placeholders() -> None:
    hostile = "Render {manifest_digest} and {0.__class__} literally in src/a.py"
    phase = TaskPhase(
        name="Phase {phase_name}",
        tasks=[SpecTask(task_id="T001", description=hostile, parallel=False, story=None, phase="x")],
    )

    prompt = build_phase_prompt(phase, "sha256:real")

    assert f"- T001: {hostile}" in prompt
    assert "Phase {phase_name}" in prompt


def test_phase_prompt_keeps_every_contract_rule() -> None:
    phase = TaskPhase(
        name="Phase 3: User Story 1",
        tasks=[
            SpecTask("T012", "Implement src/a.py", parallel=True, story="US1", phase="x"),
            SpecTask("T013", "Implement src/b.py", parallel=True, story="US1", phase="x"),
        ],
    )

    prompt = build_phase_prompt(phase, "sha256:abc")
    rules = _contract_rules(prompt)

    assert "isolated git worktree" in prompt
    assert "- T012: Implement src/a.py\n- T013: Implement src/b.py" in prompt
    assert "sha256:abc" in prompt
    assert len(rules) == 8
    _assert_contains_all(
        rules[0], "AGENTS.md", ".devplane/generated/context-implement.md", "local instructions", "before editing"
    )
    _assert_contains_all(rules[1], "this phase", "paths")
    _assert_contains_all(rules[2], "TDD", "test", "expected failure", "smallest change", "validation")
    _assert_contains_all(rules[3], "Do not modify", ".devplane/", ".specify/", "specs/", "governance")
    _assert_contains_all(rules[4], "Do not push", "deploy", "account", "destructive Git")
    _assert_contains_all(rules[5], "native validation")
    _assert_contains_all(rules[6], "focused local commit", "before finishing")
    _assert_contains_all(rules[7], "SHA", "files", "commands", "exact results", "risks")


@pytest.mark.parametrize(
    ("tasks_markdown", "expected_rules", "allows_empty_commit"),
    [
        ("## Phase 1: Setup\n- [ ] T001 Create src/service.py\n", 8, False),
        ("## Phase 1: Foundational\n- [ ] T001 Verify src/__init__.py\n", 9, True),
    ],
    ids=["implementation", "validation-only"],
)
def test_assignment_prompt_keeps_every_contract_rule(
    tasks_markdown: str, expected_rules: int, allows_empty_commit: bool
) -> None:
    prompt = _assignment_rendering(tasks_markdown)
    rules = _contract_rules(prompt)

    assert "isolated git worktree" in prompt
    assert "sha256:manifest" in prompt
    assert "- python3 -m unittest" in prompt
    assert len(rules) == expected_rules
    _assert_contains_all(
        rules[0], "AGENTS.md", ".devplane/generated/context-implement.md", "spec.md", "plan.md", "tasks.md", "before editing"
    )
    _assert_contains_all(rules[1], "allowed paths", "DevPlane will reject the commit")
    _assert_contains_all(rules[2], "TDD", "validation")
    _assert_contains_all(rules[3], "Do not modify", ".devplane/", ".git/", ".hermes/", ".specify/", "specs/")
    _assert_contains_all(rules[4], "Do not push", "deploy", "account", "destructive Git")
    _assert_contains_all(rules[5], "single focused local commit", "worktree clean")
    next_rule = 6
    if allows_empty_commit:
        _assert_contains_all(
            rules[next_rule],
            "git commit --allow-empty",
            "only if the task is exclusively validation",
            "validation passed",
        )
        next_rule += 1
    _assert_contains_all(rules[next_rule], "shell operators", "`&&`", "`||`", "`;`", "pipes", "separately")
    _assert_contains_all(rules[next_rule + 1], "SHA", "files", "commands", "results", "risks")
    assert ("--allow-empty" in prompt) is allows_empty_commit


def test_assignment_prompt_records_plan_digests_and_scope() -> None:
    plan = build_execution_plan(
        parse_tasks_markdown("## Phase 1: Setup\n- [ ] T001 Create src/service.py\n"),
        run_id="contract",
        source_tasks="specs/001/tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0",
        validation_commands=["python3 -m unittest"],
    )
    assignment = plan.assignments[0]

    prompt = _assignment_prompt(assignment, plan)

    assert f"Assignment: {assignment.assignment_id}\n" in prompt
    assert f"Phase: {assignment.phase}\n" in prompt
    assert f"Manifest digest: {plan.manifest_digest}\n" in prompt
    assert f"Tasks digest: {plan.tasks_digest}\n" in prompt
    allowed = prompt.split("Allowed write paths:\n", 1)[1].split("\n\nRequired validation", 1)[0]
    assert allowed.splitlines() == [f"- {path}" for path in assignment.allowed_paths]
