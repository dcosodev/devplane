from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devplane.agent_runtime import AgentRuntimeConfig, SessionRequest
from devplane.core import DevPlaneError
from devplane.execution_plan import build_execution_plan
from devplane.parallel import (
    _default_sessions as parallel_default_sessions,
)
from devplane.parallel import (
    build_phase_prompt,
)
from devplane.pipeline import _assignment_prompt, _default_sessions
from devplane.tasks import parse_tasks_markdown


def test_pipeline_default_sessions_dispatches_selected_runtime() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="done", stderr="")

    runtime = AgentRuntimeConfig("claude", "claude", None, "sonnet", {})
    run_many = _default_sessions(runtime)

    results = run_many(
        [SessionRequest("t1", "implement", Path("/tmp/work"))],
        max_workers=1,
        runner=runner,
    )

    assert results[0].returncode == 0
    assert calls[0][0] == "claude"


def test_pipeline_requires_explicit_runtime_for_real_default_dispatch() -> None:
    with pytest.raises(DevPlaneError, match="runtime configuration is required"):
        _default_sessions()


def test_assignment_prompt_is_agent_neutral() -> None:
    phases = parse_tasks_markdown(
        "## Phase 1: Setup\n- [ ] T001 Create src/service.py\n"
    )
    plan = build_execution_plan(
        phases,
        run_id="neutral",
        source_tasks="tasks.md",
        manifest_digest="sha256:manifest",
        base_commit="abcdef0",
        validation_commands=["python -m pytest"],
    )

    prompt = _assignment_prompt(plan.assignments[0], plan)

    assert "MiniMax" not in prompt
    assert ".hermes.md" not in prompt
    assert "agente escritor" in prompt
    assert ".devplane/generated/context-implement.md" in prompt


def test_legacy_parallel_path_dispatches_selected_runtime() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="done", stderr="")

    runtime = AgentRuntimeConfig("opencode", "opencode", None, None, {})
    run_many = parallel_default_sessions(runtime)

    run_many(
        [SessionRequest("t1", "implement", Path("/tmp/work"))],
        max_workers=1,
        runner=runner,
    )

    assert calls[0][:2] == ["opencode", "run"]


def test_legacy_parallel_requires_explicit_runtime_for_real_default_dispatch() -> None:
    with pytest.raises(DevPlaneError, match="runtime configuration is required"):
        parallel_default_sessions()


def test_legacy_phase_prompt_is_agent_neutral() -> None:
    phase = parse_tasks_markdown(
        "## Phase 1: Setup\n- [ ] T001 Create src/service.py\n"
    )[0]

    prompt = build_phase_prompt(phase, "sha256:manifest")

    assert "MiniMax" not in prompt
    assert ".hermes.md" not in prompt
    assert ".devplane/generated/context-implement.md" in prompt
