from pathlib import Path

import yaml
from typer.testing import CliRunner

from devplane.cli import app


def test_cli_help_describes_public_local_first_control_plane() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Local-first control plane for GitHub Spec Kit and Hermes Agent" in result.output
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
