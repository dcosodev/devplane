import subprocess
from pathlib import Path

from typer.testing import CliRunner

from devplane.cli import app

runner = CliRunner()


def test_init_creates_project_manifest_and_calls_speckit_runner(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("devplane.commands.run_checked", lambda args, cwd: calls.append((args, cwd)))
    monkeypatch.setattr("devplane.cli.shutil.which", lambda _: "/test-bin")
    catalog = tmp_path / "catalog"
    (catalog / "capabilities").mkdir(parents=True)
    (catalog / "manifest.yaml").write_text(
        "apiVersion: devplane.dev/v1\nkind: CapabilityCatalog\nmetadata: {name: empty}\nspec: {capabilities: []}\n",
        encoding="utf-8",
    )
    project = tmp_path / "new-repo"
    result = runner.invoke(app, ["init", str(project), "--catalog", str(catalog)])
    assert result.exit_code == 0, result.output
    assert (project / ".devplane" / "project.yaml").exists()
    rules = (project / ".hermes.md").read_text(encoding="utf-8")
    assert ".devplane/generated/context-<command>.md" in rules
    assert "resolved-manifest.yaml" in rules
    assert calls == [(["specify", "init", ".", "--integration", "hermes", "--force"], project)]


def test_init_formats_local_filesystem_errors(tmp_path: Path, monkeypatch) -> None:
    def fail_initialize(_project: Path, _catalog: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr("devplane.cli._initialize_project", fail_initialize)

    result = runner.invoke(
        app,
        ["init", str(tmp_path / "project"), "--catalog", str(tmp_path / "catalog")],
    )

    assert result.exit_code == 1
    assert "error: cannot initialize project: permission denied" in result.output
    assert "Traceback" not in result.output


def test_init_preserves_existing_hermes_rules_and_adds_contract(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("devplane.commands.run_checked", lambda args, cwd: None)
    monkeypatch.setattr("devplane.cli.shutil.which", lambda _: "/test-bin")
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "manifest.yaml").write_text(
        "apiVersion: devplane.dev/v1\nkind: CapabilityCatalog\nmetadata: {name: empty}\nspec: {capabilities: []}\n",
        encoding="utf-8",
    )
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".hermes.md").write_text("# Existing rules\n\nKeep me.\n", encoding="utf-8")
    result = runner.invoke(app, ["init", str(project), "--catalog", str(catalog)])
    assert result.exit_code == 0, result.output
    rules = (project / ".hermes.md").read_text(encoding="utf-8")
    assert "Keep me." in rules
    assert rules.count("BEGIN DEVPLANE MANAGED CONTRACT") == 1
    assert "resolved-manifest.yaml" in rules


def test_validate_reports_missing_speckit_layout(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".devplane").mkdir(parents=True)
    (project / ".devplane" / "project.yaml").write_text(
        "apiVersion: devplane.dev/v1\nkind: AgentProject\nmetadata: {name: demo}\nspec:\n  catalog: {source: ../catalog}\n  capabilities: []\n  workflow: {engine: speckit}\n  runtime: {agent: hermes}\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate", "--project", str(project)])
    assert result.exit_code != 0
    assert ".specify" in result.output


def test_validate_detects_context_drift(tmp_path: Path) -> None:
    from test_sync import make_project

    project = make_project(tmp_path)
    (project / ".specify").mkdir()
    (project / ".specify" / "integration.json").write_text(
        '{"version":"0.14.5","installed_integrations":["hermes"],"default_integration":"hermes"}',
        encoding="utf-8",
    )
    (project / ".hermes" / "skills").mkdir(parents=True)
    assert runner.invoke(app, ["sync", "--project", str(project)]).exit_code == 0
    (project / ".devplane" / "generated" / "context-plan.md").write_text("drift", encoding="utf-8")
    result = runner.invoke(app, ["validate", "--project", str(project)])
    assert result.exit_code == 1
    assert "context drift" in result.output


def test_validate_rejects_marker_without_hermes_integration_state(tmp_path: Path) -> None:
    from test_sync import make_project

    project = make_project(tmp_path)
    (project / ".specify").mkdir()
    (project / ".hermes" / "skills").mkdir(parents=True)
    assert runner.invoke(app, ["sync", "--project", str(project)]).exit_code == 0
    result = runner.invoke(app, ["validate", "--project", str(project)])
    assert result.exit_code == 1
    assert "integration.json" in result.output


def test_run_dispatches_native_speckit_workflow_to_hermes(tmp_path: Path, monkeypatch) -> None:
    from test_sync import make_project

    project = make_project(tmp_path)
    (project / ".specify").mkdir()
    (project / ".hermes" / "skills").mkdir(parents=True)
    runner.invoke(app, ["sync", "--project", str(project)])
    calls = []

    def fake_run(args, cwd):
        calls.append((args, cwd))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout='{"run_id":"abc","status":"paused","workflow":"untrusted"}\n',
        )

    monkeypatch.setattr("devplane.commands.run_checked", fake_run)
    result = runner.invoke(
        app,
        ["run", "Build secure batch payments", "--project", str(project)],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        ([
            "specify", "workflow", "run", "speckit",
            "--input", "spec=Build secure batch payments",
            "--input", "integration=hermes", "--json",
        ], project)
    ]
    audit = (project / ".devplane" / "audit" / "runs.jsonl").read_text()
    assert '"run_id": "abc"' in audit
    assert '"workflow": "speckit"' in audit
    assert '"workflow": "untrusted"' not in audit


def test_resume_dispatches_speckit_and_records_result(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    calls = []

    def fake_run(args, cwd):
        calls.append((args, cwd))
        return subprocess.CompletedProcess(args, 0, stdout='{"run_id":"abc","status":"completed"}\n')

    monkeypatch.setattr("devplane.commands.run_checked", fake_run)
    result = runner.invoke(app, ["resume", "abc", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert calls == [(["specify", "workflow", "resume", "abc", "--json"], project)]
    audit = (project / ".devplane" / "audit" / "runs.jsonl").read_text()
    assert '"operation": "resume"' in audit


def test_run_refuses_stale_context(tmp_path: Path, monkeypatch) -> None:
    from test_sync import make_project

    project = make_project(tmp_path)
    runner.invoke(app, ["sync", "--project", str(project)])
    (project / ".devplane" / "generated" / "context-plan.md").write_text("drift", encoding="utf-8")
    calls = []
    monkeypatch.setattr("devplane.commands.run_checked", lambda args, cwd: calls.append((args, cwd)))
    result = runner.invoke(app, ["run", "Build feature", "--project", str(project)])
    assert result.exit_code == 1
    assert "context drift" in result.output
    assert calls == []


def test_implement_parallel_dry_run_plans_speckit_tasks(tmp_path: Path) -> None:
    from test_sync import make_project

    project = make_project(tmp_path)
    assert runner.invoke(app, ["sync", "--project", str(project)]).exit_code == 0
    tasks_file = project / "specs" / "001-demo" / "tasks.md"
    tasks_file.parent.mkdir(parents=True)
    tasks_file.write_text(
        "## Phase 3: User Story 1\n"
        "- [ ] T012 [P] [US1] Implement service in src/service.py\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "implement",
            "--parallel",
            "--dry-run",
            "--tasks-file",
            str(tasks_file),
            "--project",
            str(project),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Phase 3: User Story 1" in result.output
    assert "planned" in result.output
    assert not (tmp_path / ".devplane-worktrees").exists()


def test_plan_execution_writes_governed_contract_from_committed_tasks(tmp_path: Path) -> None:
    from test_sync import make_project

    project = make_project(tmp_path)
    assert runner.invoke(app, ["sync", "--project", str(project)]).exit_code == 0
    tasks_file = project / "specs" / "001-demo" / "tasks.md"
    tasks_file.parent.mkdir(parents=True)
    tasks_file.write_text(
        "## Phase 3: User Story 1\n"
        "- [ ] T012 [US1] Implement service in src/service.py\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "DevPlane Test"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "devplane@test.invalid"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "test: approved plan"], cwd=project, check=True, capture_output=True)

    result = runner.invoke(
        app,
        [
            "plan-execution",
            "--tasks-file",
            str(tasks_file),
            "--validation",
            "uv run pytest",
            "--run-id",
            "feature-001",
            "--project",
            str(project),
        ],
    )

    assert result.exit_code == 0, result.output
    plan = project / ".git" / "devplane" / "runs" / "feature-001" / "execution-plan.yaml"
    assert plan.is_file()
    body = plan.read_text(encoding="utf-8")
    assert "kind: ExecutionPlan" in body
    assert "src/service.py" in body
    assert "uv run pytest" in body
    assert "feature-001" in result.output

    dry_run = runner.invoke(
        app,
        [
            "implement",
            "--parallel",
            "--dry-run",
            "--execution-plan",
            str(plan),
            "--project",
            str(project),
        ],
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert '"assignment_id": "p1-phase-3-user-story-1"' in dry_run.output

    status = runner.invoke(app, ["run-status", "feature-001", "--project", str(project)])
    assert status.exit_code == 0, status.output
    assert '"status": "planned"' in status.output
