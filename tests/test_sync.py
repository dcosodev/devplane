from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from devplane.cli import app
from devplane.core import DevPlaneError, build_resolved_manifest, sync_project

runner = CliRunner()


def make_project(tmp_path: Path) -> Path:
    catalog = tmp_path / "catalog"
    capability = catalog / "capabilities" / "corp-base"
    capability.mkdir(parents=True)
    (catalog / "manifest.yaml").write_text(
        "apiVersion: devplane.dev/v1\nkind: CapabilityCatalog\nmetadata:\n  name: corp\nspec:\n  capabilities:\n    - ref: capabilities/corp-base/capability.yaml\n",
        encoding="utf-8",
    )
    (capability / "instructions.md").write_text("Use small changes.\n", encoding="utf-8")
    (capability / "capability.yaml").write_text(
        "apiVersion: devplane.dev/v1\nkind: Capability\nmetadata:\n  id: corp-base\n  version: 1.0.0\nspec:\n  context:\n    plan:\n      include: [instructions.md]\n  permissions:\n    write: [specs/**]\n    shell:\n      allow: [pytest*]\n",
        encoding="utf-8",
    )
    project = tmp_path / "repo"
    (project / ".devplane").mkdir(parents=True)
    (project / ".devplane" / "project.yaml").write_text(
        "apiVersion: devplane.dev/v1\nkind: AgentProject\nmetadata:\n  name: demo\nspec:\n  catalog:\n    source: ../catalog\n  capabilities: [corp-base@1.0.0]\n  workflow:\n    engine: speckit\n  runtime:\n    agent: hermes\n",
        encoding="utf-8",
    )
    return project


def test_sync_is_deterministic_and_check_detects_drift(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    first = sync_project(project)
    generated = project / ".devplane" / "generated" / "resolved-manifest.yaml"
    first_bytes = generated.read_bytes()
    second = sync_project(project)
    assert first == second
    assert first_bytes == generated.read_bytes()
    generated.write_text("drift", encoding="utf-8")
    with pytest.raises(DevPlaneError, match="drift"):
        sync_project(project, check=True)


def test_manifest_has_exact_digest_and_no_timestamp(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    resolved = build_resolved_manifest(project)
    assert "generatedAt" not in resolved["metadata"]
    assert resolved["spec"]["workflow"]["engine"] == "speckit"
    assert resolved["spec"]["runtime"]["agent"] == "hermes"
    assert len(resolved["metadata"]["sourceHash"].removeprefix("sha256:")) == 64
    rendered = yaml.safe_dump(resolved)
    assert str(tmp_path) not in rendered


def test_sync_rejects_catalog_escape(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    cfg_path = project / ".devplane" / "project.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["spec"]["catalog"]["source"] = "../../outside"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(DevPlaneError, match="catalog source"):
        build_resolved_manifest(project)


def test_sync_rejects_symlinked_context_escape(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    resource = tmp_path / "catalog" / "capabilities" / "corp-base" / "instructions.md"
    resource.unlink()
    resource.symlink_to(outside)
    with pytest.raises(DevPlaneError, match="context resource"):
        build_resolved_manifest(project)


def test_cli_sync_and_check(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    result = runner.invoke(app, ["sync", "--project", str(project)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["sync", "--check", "--project", str(project)])
    assert result.exit_code == 0, result.output
    context = project / ".devplane" / "generated" / "context-plan.md"
    context.write_text("drift", encoding="utf-8")
    result = runner.invoke(app, ["sync", "--check", "--project", str(project)])
    assert result.exit_code == 1
    assert "context drift" in result.output


def test_sync_removes_stale_generated_context(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    assert runner.invoke(app, ["sync", "--project", str(project)]).exit_code == 0
    cfg_path = project / ".devplane" / "project.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["spec"]["capabilities"] = []
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    result = runner.invoke(app, ["sync", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert not (project / ".devplane" / "generated" / "context-plan.md").exists()
