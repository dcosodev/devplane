from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml
from typer.testing import CliRunner

from devplane.cli import app
from devplane.discovery import discover_repository

runner = CliRunner()


def test_discovery_profiles_python_node_monorepo_deterministically(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    (project / "src").mkdir()
    (project / "frontend").mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\ndependencies = ['fastapi']\n[dependency-groups]\ndev = ['pytest']\n",
        encoding="utf-8",
    )
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (project / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"test": "vitest", "lint": "eslint ."},
                "dependencies": {"react": "latest"},
            }
        ),
        encoding="utf-8",
    )
    (project / "pnpm-lock.yaml").write_text("lockfileVersion: '9'\n", encoding="utf-8")

    first = discover_repository(project)
    second = discover_repository(project)

    assert first == second
    assert first["languages"] == ["javascript", "python"]
    assert first["packageManagers"] == ["pnpm", "uv"]
    assert first["frameworks"] == ["fastapi", "pytest", "react"]
    assert first["validation"] == ["uv run pytest", "pnpm test", "pnpm run lint"]
    assert first["architecture"] == ["frontend", "src"]
    assert first["digest"].startswith("sha256:")


def test_profile_approve_records_digest_and_regenerates_manifest(tmp_path: Path) -> None:
    from test_sync import make_project

    project = make_project(tmp_path)
    assert runner.invoke(app, ["sync", "--project", str(project)]).exit_code == 0
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True)
    (project / "pyproject.toml").write_text("[project]\ndependencies = ['fastapi', 'pytest']\n", encoding="utf-8")
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    result = runner.invoke(app, ["profile", "--approve", "--project", str(project)])

    assert result.exit_code == 0, result.output
    config = yaml.safe_load((project / ".devplane" / "project.yaml").read_text(encoding="utf-8"))
    approved = config["spec"]["repositoryProfile"]
    assert approved["approved"] is True
    assert approved["digest"].startswith("sha256:")
    assert approved["validation"] == ["uv run pytest"]
    resolved = yaml.safe_load(
        (project / ".devplane" / "generated" / "resolved-manifest.yaml").read_text(encoding="utf-8")
    )
    assert resolved["spec"]["repositoryProfile"] == approved
    assert (project / ".git" / "devplane" / "repository-profile.yaml").is_file()


def test_activate_capability_updates_project_and_resolved_context(tmp_path: Path) -> None:
    from test_sync import make_project

    project = make_project(tmp_path)
    config_path = project / ".devplane" / "project.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["spec"]["capabilities"] = []
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    result = runner.invoke(
        app,
        ["activate", "corp-base@1.0.0", "--project", str(project)],
    )

    assert result.exit_code == 0, result.output
    updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert updated["spec"]["capabilities"] == ["corp-base@1.0.0"]
    resolved = yaml.safe_load(
        (project / ".devplane" / "generated" / "resolved-manifest.yaml").read_text(encoding="utf-8")
    )
    assert resolved["spec"]["activeCapabilities"][0]["id"] == "corp-base"
    assert (project / ".devplane" / "generated" / "context-plan.md").is_file()
