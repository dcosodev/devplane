from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from devplane.cli import app
from devplane.core import DevPlaneError
from devplane.sdd import (
    FeatureState,
    execute_phase,
    load_feature_state,
    write_feature_state,
    write_phase_workflow,
)

runner = CliRunner()


def test_phase_workflow_dispatches_exactly_one_speckit_skill_without_json_mode(tmp_path: Path) -> None:
    path = write_phase_workflow(tmp_path, "feature-1", "plan")
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert workflow["workflow"]["description"] == "Invoke only /speckit-plan under DevPlane gates"
    assert workflow["steps"] == [
        {
            "id": "plan",
            "type": "prompt",
            "prompt": "/speckit-plan {{ inputs.spec }}",
            "integration": "{{ inputs.integration }}",
            "model": "MiniMax-M3",
            "timeout": 900,
        }
    ]


def test_execute_phase_reads_valid_speckit_json_among_streamed_output(
    tmp_path: Path, monkeypatch
) -> None:
    observed_extra_args: list[str | None] = []
    monkeypatch.setenv("SPECKIT_INTEGRATION_HERMES_EXTRA_ARGS", "--provider existing")

    def fake_run(
        args: list[str], cwd: Path, environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        observed_extra_args.append(environment.get("SPECKIT_INTEGRATION_HERMES_EXTRA_ARGS"))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                "session_id: 20260731_example\n"
                '{"run_id":"agent-spoof","status":"thinking"}\n'
                "Created the requested artifact.\n"
                '{"run_id":"remote-specify","status":"completed"}\n'
                "stream closed\n"
            ),
        )

    result = execute_phase(
        tmp_path,
        "feature-streamed",
        "specify",
        "Build a tiny feature",
        command_runner=fake_run,
    )

    assert result["run_id"] == "remote-specify"
    assert result["status"] == "completed"
    assert observed_extra_args == ["--provider minimax-oauth"]
    assert os.environ["SPECKIT_INTEGRATION_HERMES_EXTRA_ARGS"] == "--provider existing"


def test_execute_phase_rejects_stream_without_a_completed_speckit_result(tmp_path: Path) -> None:
    def fake_run(
        args: list[str], cwd: Path, environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args,
            0,
            stdout='agent output\n{"run_id":"agent-value","status":"thinking"}\n',
        )

    with pytest.raises(DevPlaneError, match="invalid Spec Kit JSON output"):
        execute_phase(
            tmp_path,
            "feature-invalid-stream",
            "specify",
            "Build a tiny feature",
            command_runner=fake_run,
        )


def test_run_stop_after_tasks_persists_spec_approval_gate(tmp_path: Path, monkeypatch) -> None:
    from test_sync import make_project

    project = make_project(tmp_path)
    assert runner.invoke(app, ["sync", "--project", str(project)]).exit_code == 0
    calls: list[tuple[list[str], Path]] = []

    def fake_run(args, cwd, *, environment):
        assert environment["SPECKIT_INTEGRATION_HERMES_EXTRA_ARGS"] == "--provider minimax-oauth"
        calls.append((args, cwd))
        return subprocess.CompletedProcess(args, 0, stdout='{"run_id":"spec-remote","status":"completed"}\n')

    monkeypatch.setattr("devplane.commands.run_checked", fake_run)
    result = runner.invoke(
        app,
        [
            "run",
            "Build secure reservations",
            "--stop-after",
            "tasks",
            "--run-id",
            "feature-1",
            "--project",
            str(project),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0][:3] == ["specify", "workflow", "run"]
    assert calls[0][0][3].endswith("phase-specify.yaml")
    assert "--json" in calls[0][0]
    state = load_feature_state(project, "feature-1")
    assert state.status == "spec_pending_approval"
    assert state.external_runs == {"specify": "spec-remote"}
    status = runner.invoke(app, ["run-status", "feature-1", "--project", str(project)])
    assert status.exit_code == 0, status.output
    assert '"status": "spec_pending_approval"' in status.output
    request_path = project / ".git" / "devplane" / "runs" / "feature-1" / "request.txt"
    assert request_path.read_text(encoding="utf-8") == "Build secure reservations"
    assert "Build secure reservations" not in json.dumps(state.to_mapping())


def test_approve_advances_spec_then_plan_then_tasks_without_implement(tmp_path: Path, monkeypatch) -> None:
    from test_sync import make_project

    project = make_project(tmp_path)
    assert runner.invoke(app, ["sync", "--project", str(project)]).exit_code == 0
    calls: list[list[str]] = []

    def fake_run(args, cwd, *, environment):
        assert environment["SPECKIT_INTEGRATION_HERMES_EXTRA_ARGS"] == "--provider minimax-oauth"
        calls.append(args)
        phase = Path(args[3]).stem.removeprefix("phase-")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"run_id": f"{phase}-remote", "status": "completed"}))

    monkeypatch.setattr("devplane.commands.run_checked", fake_run)
    started = runner.invoke(
        app,
        ["run", "Build demo", "--stop-after", "tasks", "--run-id", "feature-2", "--project", str(project)],
    )
    assert started.exit_code == 0, started.output

    approved_spec = runner.invoke(app, ["approve", "feature-2", "spec", "--project", str(project)])
    assert approved_spec.exit_code == 0, approved_spec.output
    assert load_feature_state(project, "feature-2").status == "plan_pending_approval"

    approved_plan = runner.invoke(app, ["approve", "feature-2", "plan", "--project", str(project)])
    assert approved_plan.exit_code == 0, approved_plan.output
    assert load_feature_state(project, "feature-2").status == "tasks_pending_approval"

    assert [Path(call[3]).name for call in calls] == [
        "phase-specify.yaml",
        "phase-plan.yaml",
        "phase-tasks.yaml",
    ]
    assert all("implement" not in Path(call[3]).read_text(encoding="utf-8") for call in calls)


def test_checkpoint_commits_approved_artifacts_and_creates_execution_plan(tmp_path: Path) -> None:
    from test_sync import make_project

    project = make_project(tmp_path)
    assert runner.invoke(app, ["sync", "--project", str(project)]).exit_code == 0
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "DevPlane Test"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "devplane@test.invalid"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "test: bootstrap"], cwd=project, check=True, capture_output=True)
    feature = project / "specs" / "001-demo"
    feature.mkdir(parents=True)
    (feature / "spec.md").write_text("# Spec\n", encoding="utf-8")
    (feature / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (feature / "tasks.md").write_text(
        "## Phase 3: User Story 1\n- [ ] T001 [US1] Implement src/demo.py\n",
        encoding="utf-8",
    )
    write_feature_state(
        project,
        FeatureState(
            run_id="feature-3",
            status="ready_to_checkpoint",
            manifest_digest="sha256:placeholder",
            spec_digest="sha256:request",
            external_runs={"specify": "a", "plan": "b", "tasks": "c"},
        ),
    )
    # Bind the state to the actual synchronized manifest.
    import yaml

    resolved = yaml.safe_load(
        (project / ".devplane" / "generated" / "resolved-manifest.yaml").read_text(encoding="utf-8")
    )
    state = load_feature_state(project, "feature-3")
    write_feature_state(
        project,
        FeatureState(
            state.run_id,
            state.status,
            resolved["metadata"]["sourceHash"],
            state.spec_digest,
            state.external_runs,
        ),
    )

    result = runner.invoke(
        app,
        [
            "checkpoint",
            "feature-3",
            "--tasks-file",
            str(feature / "tasks.md"),
            "--validation",
            "python3 -m unittest",
            "--project",
            str(project),
        ],
    )

    assert result.exit_code == 0, result.output
    assert load_feature_state(project, "feature-3").status == "ready_to_implement"
    assert (project / ".git" / "devplane" / "runs" / "feature-3" / "execution-plan.yaml").is_file()
    committed = subprocess.run(
        ["git", "show", "--name-only", "--format="], cwd=project, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    assert set(committed) == {
        "specs/001-demo/spec.md",
        "specs/001-demo/plan.md",
        "specs/001-demo/tasks.md",
    }
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=project, check=True, capture_output=True, text=True
    ).stdout == ""
