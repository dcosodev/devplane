from __future__ import annotations

import subprocess
from pathlib import Path

import yaml
from typer.testing import CliRunner

from devplane.cli import app
from devplane.pipeline import PipelineRun

runner = CliRunner()


def _catalog(tmp_path: Path) -> Path:
    catalog = tmp_path / "catalog"
    capability = catalog / "capabilities" / "base"
    capability.mkdir(parents=True)
    (catalog / "manifest.yaml").write_text(
        "apiVersion: devplane.dev/v1\n"
        "kind: CapabilityCatalog\n"
        "metadata:\n  name: organization\n"
        "spec:\n"
        "  capabilities:\n"
        "    - ref: capabilities/base/capability.yaml\n"
        "  profiles:\n"
        "    - id: general-development\n"
        "      capabilities: [base@1.0.0]\n",
        encoding="utf-8",
    )
    (capability / "capability.yaml").write_text(
        "apiVersion: devplane.dev/v1\n"
        "kind: Capability\n"
        "metadata:\n  id: base\n  version: 1.0.0\n"
        "spec:\n"
        "  context: {}\n"
        "  permissions: {}\n"
        "  validations: [python -m pytest]\n",
        encoding="utf-8",
    )
    return catalog


def test_adapters_command_lists_supported_runtime_contracts() -> None:
    result = runner.invoke(app, ["adapters"])

    assert result.exit_code == 0, result.output
    payload = yaml.safe_load(result.output)
    assert [item["id"] for item in payload["adapters"]] == [
        "claude",
        "hermes",
        "opencode",
    ]
    assert all("executable" in item for item in payload["adapters"])


def test_init_catalog_only_needs_no_spec_kit_or_agent(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    catalog = _catalog(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "devplane.commands.run_checked", lambda args, cwd: calls.append(args)
    )
    monkeypatch.setattr("devplane.cli.shutil.which", lambda name: None)

    result = runner.invoke(
        app,
        [
            "init",
            str(project),
            "--catalog",
            str(catalog),
            "--workflow",
            "none",
            "--runtime",
            "none",
        ],
    )

    assert result.exit_code == 0, result.output
    config = yaml.safe_load(
        (project / ".devplane" / "project.yaml").read_text(encoding="utf-8")
    )
    assert "workflow" not in config["spec"]
    assert "runtime" not in config["spec"]
    assert not (project / ".hermes.md").exists()
    assert calls == []


def test_runtime_command_selects_agent_without_changing_catalog(tmp_path: Path) -> None:
    project = tmp_path / "project"
    catalog = _catalog(tmp_path)
    initialized = runner.invoke(
        app,
        [
            "init",
            str(project),
            "--catalog",
            str(catalog),
            "--workflow",
            "none",
            "--runtime",
            "none",
        ],
    )
    assert initialized.exit_code == 0, initialized.output

    result = runner.invoke(
        app,
        [
            "runtime",
            "claude",
            "--model",
            "sonnet",
            "--project",
            str(project),
        ],
    )

    assert result.exit_code == 0, result.output
    config = yaml.safe_load(
        (project / ".devplane" / "project.yaml").read_text(encoding="utf-8")
    )
    assert config["spec"]["runtime"] == {
        "adapter": "claude",
        "model": "sonnet",
    }
    resolved = yaml.safe_load(
        (project / ".devplane" / "generated" / "resolved-manifest.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert resolved["spec"]["runtime"]["adapter"] == "claude"


def test_use_profile_selects_reusable_catalog_profile(tmp_path: Path) -> None:
    project = tmp_path / "project"
    catalog = _catalog(tmp_path)
    initialized = runner.invoke(
        app,
        [
            "init",
            str(project),
            "--catalog",
            str(catalog),
            "--workflow",
            "none",
            "--runtime",
            "none",
        ],
    )
    assert initialized.exit_code == 0, initialized.output

    result = runner.invoke(
        app,
        ["use-profile", "general-development", "--project", str(project)],
    )

    assert result.exit_code == 0, result.output
    resolved = yaml.safe_load(
        (project / ".devplane" / "generated" / "resolved-manifest.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert resolved["spec"]["selectedProfile"] == "general-development"
    assert resolved["spec"]["activeCapabilities"][0]["id"] == "base"


def test_runtime_command_rejects_unknown_adapter_without_mutation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    catalog = _catalog(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            str(project),
            "--catalog",
            str(catalog),
            "--workflow",
            "none",
            "--runtime",
            "hermes",
        ],
    )
    assert result.exit_code == 0, result.output
    config_path = project / ".devplane" / "project.yaml"
    before = config_path.read_bytes()

    result = runner.invoke(
        app,
        ["runtime", "unknown", "--project", str(project)],
    )

    assert result.exit_code == 1
    assert "unsupported agent adapter" in result.output
    assert config_path.read_bytes() == before


def test_runtime_command_rolls_back_when_sync_fails(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    catalog = _catalog(tmp_path)
    initialized = runner.invoke(
        app,
        [
            "init",
            str(project),
            "--catalog",
            str(catalog),
            "--workflow",
            "none",
            "--runtime",
            "none",
        ],
    )
    assert initialized.exit_code == 0, initialized.output
    config_path = project / ".devplane" / "project.yaml"
    before = config_path.read_bytes()

    def fail_sync(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("devplane.cli.sync_project", fail_sync)
    result = runner.invoke(
        app,
        ["runtime", "claude", "--project", str(project)],
    )

    assert result.exit_code == 1
    assert "cannot select runtime" in result.output
    assert config_path.read_bytes() == before


def test_new_catalog_project_can_use_opencode_without_spec_kit(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "new-project"
    catalog = _catalog(tmp_path)
    monkeypatch.setattr(
        "devplane.cli.shutil.which",
        lambda name: "/usr/bin/git" if name == "git" else None,
    )

    result = runner.invoke(
        app,
        [
            "new",
            str(project),
            "--catalog",
            str(catalog),
            "--workflow",
            "none",
            "--runtime",
            "opencode",
        ],
    )

    assert result.exit_code == 0, result.output
    config = yaml.safe_load(
        (project / ".devplane" / "project.yaml").read_text(encoding="utf-8")
    )
    assert config["spec"]["runtime"] == {"adapter": "opencode"}
    assert "workflow" not in config["spec"]
    assert not (project / ".specify").exists()


def test_new_rejects_invalid_modes_before_creating_project(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    for option, value in (("--workflow", "unknown"), ("--runtime", "unknown")):
        project = tmp_path / f"invalid-{option[2:]}"

        result = runner.invoke(
            app,
            ["new", str(project), "--catalog", str(catalog), option, value],
        )

        assert result.exit_code == 1
        assert "unsupported" in result.output
        assert not project.exists()


def test_validate_accepts_catalog_only_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    catalog = _catalog(tmp_path)
    initialized = runner.invoke(
        app,
        [
            "init",
            str(project),
            "--catalog",
            str(catalog),
            "--workflow",
            "none",
            "--runtime",
            "none",
        ],
    )
    assert initialized.exit_code == 0, initialized.output
    assert runner.invoke(app, ["sync", "--project", str(project)]).exit_code == 0

    result = runner.invoke(app, ["validate", "--project", str(project)])

    assert result.exit_code == 0, result.output
    assert "valid" in result.output


def test_catalog_only_execution_fails_before_creating_run_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    catalog = _catalog(tmp_path)
    initialized = runner.invoke(
        app,
        [
            "init",
            str(project),
            "--catalog",
            str(catalog),
            "--workflow",
            "none",
            "--runtime",
            "none",
        ],
    )
    assert initialized.exit_code == 0, initialized.output
    assert runner.invoke(app, ["sync", "--project", str(project)]).exit_code == 0
    tasks = project / "tasks.md"
    tasks.write_text(
        "## Phase 1: Setup\n- [ ] T001 Create docs/guide.md\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "implement",
            "--parallel",
            "--tasks-file",
            "tasks.md",
            "--project",
            str(project),
        ],
    )

    assert result.exit_code == 1
    assert "no agent runtime configured" in result.output
    assert not (project / ".devplane" / "runs").exists()


def test_implement_passes_selected_runtime_to_execution_pipeline(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    catalog = _catalog(tmp_path)
    initialized = runner.invoke(
        app,
        [
            "init",
            str(project),
            "--catalog",
            str(catalog),
            "--workflow",
            "none",
            "--runtime",
            "claude",
        ],
    )
    assert initialized.exit_code == 0, initialized.output
    selected = runner.invoke(
        app,
        ["use-profile", "general-development", "--project", str(project)],
    )
    assert selected.exit_code == 0, selected.output
    configured = runner.invoke(
        app,
        [
            "runtime",
            "claude",
            "--model",
            "sonnet",
            "--project",
            str(project),
        ],
    )
    assert configured.exit_code == 0, configured.output
    tasks = project / "tasks.md"
    tasks.write_text(
        "## Phase 1: Setup\n- [ ] T001 Create src/service.py\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "test: approved inputs"], cwd=project, check=True)
    planned = runner.invoke(
        app,
        [
            "plan-execution",
            "--tasks-file",
            "tasks.md",
            "--run-id",
            "multi-agent",
            "--project",
            str(project),
        ],
    )
    assert planned.exit_code == 0, planned.output
    plan_path = project / ".git" / "devplane" / "runs" / "multi-agent" / "execution-plan.yaml"
    assert "python -m pytest" in plan_path.read_text(encoding="utf-8")
    captured = {}

    def fake_pipeline(project, plan, *, max_agents, runtime_config=None, **kwargs):
        captured["runtime"] = runtime_config
        return PipelineRun(plan.run_id, "completed", plan.base_commit, plan.base_commit, None, None, ())

    monkeypatch.setattr("devplane.cli.run_execution_pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        [
            "implement",
            "--parallel",
            "--execution-plan",
            str(plan_path),
            "--project",
            str(project),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["runtime"].adapter == "claude"
    assert captured["runtime"].model == "sonnet"
