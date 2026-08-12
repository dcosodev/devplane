import tomllib
from pathlib import Path

import yaml
from typer.testing import CliRunner

from devplane import __version__
from devplane.cli import app


def test_cli_help_describes_public_catalog_and_multi_agent_control_plane() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "organizational catalog" in result.output
    assert "multi-agent development control plane" in result.output
    assert "Private control plane" not in result.output


def test_readme_activation_matches_public_example_catalog() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "examples/catalog/manifest.yaml").read_text(encoding="utf-8"))
    capability_path = root / "examples/catalog" / manifest["spec"]["capabilities"][0]["ref"]
    capability = yaml.safe_load(capability_path.read_text(encoding="utf-8"))
    identifier = capability["metadata"]["id"]
    version = capability["metadata"]["version"]
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert f"devplane activate {identifier}@{version}" in readme


def test_public_catalog_demonstrates_multiple_reusable_development_profiles() -> None:
    root = Path(__file__).resolve().parents[1]
    catalog_root = root / "examples/catalog"
    manifest = yaml.safe_load((catalog_root / "manifest.yaml").read_text(encoding="utf-8"))
    profiles = manifest["spec"]["profiles"]
    capability_ids = {}
    for entry in manifest["spec"]["capabilities"]:
        capability = yaml.safe_load((catalog_root / entry["ref"]).read_text(encoding="utf-8"))
        capability_ids[capability["metadata"]["id"]] = capability["metadata"]["version"]

    assert {profile["id"] for profile in profiles} == {
        "documentation",
        "general-development",
        "python-service",
        "web-frontend",
    }
    for profile in profiles:
        for request in profile["capabilities"]:
            identifier, version = request.rsplit("@", 1)
            assert capability_ids[identifier] == version


def test_public_metadata_and_readme_present_agent_neutral_v02x() -> None:
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert metadata["project"]["version"] == "0.2.1"
    assert "organizational capability catalog" in metadata["project"]["description"].lower()
    for adapter in ("Hermes", "Claude Code", "OpenCode"):
        assert adapter in readme
    assert "Catalog" in readme
    assert "Control plane" in readme
    assert "Runtime adapters" in readme


def test_module_version_matches_package_metadata() -> None:
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert __version__ == metadata["project"]["version"]
