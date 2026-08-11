from typer.testing import CliRunner

from devplane.cli import app


def test_cli_help_describes_public_local_first_control_plane() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Local-first control plane for GitHub Spec Kit and Hermes Agent" in result.output
    assert "Private control plane" not in result.output
