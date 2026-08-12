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


def test_catalog_profile_resolves_without_workflow_or_agent_runtime(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    catalog = tmp_path / "catalog"
    quality = catalog / "capabilities" / "python-quality"
    quality.mkdir()
    (quality / "instructions.md").write_text("Use pytest and Ruff.\n", encoding="utf-8")
    (quality / "capability.yaml").write_text(
        "apiVersion: devplane.dev/v1\n"
        "kind: Capability\n"
        "metadata:\n  id: python-quality\n  version: 2.0.0\n"
        "spec:\n"
        "  context:\n    implement:\n      include: [instructions.md]\n"
        "  permissions:\n    write: [src/**, tests/**]\n"
        "  validations: [uv run pytest, uv run ruff check src tests]\n",
        encoding="utf-8",
    )
    manifest_path = catalog / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["spec"]["capabilities"].append(
        {"ref": "capabilities/python-quality/capability.yaml"}
    )
    manifest["spec"]["profiles"] = [
        {
            "id": "python-service",
            "capabilities": ["corp-base@1.0.0", "python-quality@2.0.0"],
        }
    ]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    config_path = project / ".devplane" / "project.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["spec"]["profile"] = "python-service"
    config["spec"]["capabilities"] = []
    config["spec"].pop("workflow")
    config["spec"].pop("runtime")
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    resolved = build_resolved_manifest(project)

    assert resolved["spec"]["selectedProfile"] == "python-service"
    assert [item["id"] for item in resolved["spec"]["activeCapabilities"]] == [
        "corp-base",
        "python-quality",
    ]
    assert resolved["spec"]["validations"] == [
        "uv run pytest",
        "uv run ruff check src tests",
    ]
    assert "workflow" not in resolved["spec"]
    assert "runtime" not in resolved["spec"]


def test_organizational_catalog_can_live_outside_project_parent(tmp_path: Path) -> None:
    project = make_project(tmp_path / "workspace")
    original_catalog = tmp_path / "workspace" / "catalog"
    organization_catalog = tmp_path / "shared" / "engineering-catalog"
    organization_catalog.parent.mkdir(parents=True)
    original_catalog.rename(organization_catalog)
    config_path = project / ".devplane" / "project.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["spec"]["catalog"]["source"] = str(organization_catalog)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    resolved = build_resolved_manifest(project)

    assert resolved["spec"]["catalog"]["source"] == str(organization_catalog)
    assert resolved["spec"]["activeCapabilities"][0]["id"] == "corp-base"


def test_catalog_root_symlink_is_rejected(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    config_path = project / ".devplane" / "project.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    real_catalog = project / config["spec"]["catalog"]["source"]
    catalog_link = tmp_path / "catalog-link"
    catalog_link.symlink_to(real_catalog.resolve(), target_is_directory=True)
    config["spec"]["catalog"]["source"] = str(catalog_link)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    with pytest.raises(DevPlaneError, match="catalog root must not be a symlink"):
        build_resolved_manifest(project)


@pytest.mark.parametrize(
    ("section", "payload", "message"),
    [
        ("workflow", {"engine": "unknown"}, "unsupported workflow engine: unknown"),
        ("runtime", {"adapter": "unknown"}, "unsupported agent adapter: unknown"),
    ],
)
def test_manifest_rejects_unsupported_execution_plugins(
    tmp_path: Path,
    section: str,
    payload: dict[str, str],
    message: str,
) -> None:
    project = make_project(tmp_path)
    config_path = project / ".devplane" / "project.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["spec"][section] = payload
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    with pytest.raises(DevPlaneError, match=message):
        build_resolved_manifest(project)


def test_sync_reports_missing_explicit_external_catalog(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    cfg_path = project / ".devplane" / "project.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["spec"]["catalog"]["source"] = "../../outside"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(DevPlaneError, match="required file not found"):
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
