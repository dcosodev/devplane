from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from devplane.cli import app

runner = CliRunner()


def _catalog(tmp_path: Path) -> Path:
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "manifest.yaml").write_text(
        "apiVersion: devplane.dev/v1\nkind: CapabilityCatalog\nmetadata:\n  name: test\nspec:\n  capabilities: []\n",
        encoding="utf-8",
    )
    return catalog


def _install_specify_stub(tmp_path: Path, monkeypatch, *, create_env: bool = False) -> None:
    binary = tmp_path / "bin" / "specify"
    binary.parent.mkdir()
    payload = json.dumps(
        {
            "installed_integrations": ["hermes"],
            "default_integration": "hermes",
            "version": "0.14.0",
        }
    )
    env_line = "Path('.env').write_text('SECRET=x')" if create_env else ""
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        "Path('.specify').mkdir(exist_ok=True)\n"
        "Path('.hermes/skills').mkdir(parents=True, exist_ok=True)\n"
        f"Path('.specify/integration.json').write_text({payload!r})\n"
        f"{env_line}\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ['PATH']}")


def test_new_bootstraps_valid_greenfield_project_and_git_baseline(tmp_path: Path, monkeypatch) -> None:
    catalog = _catalog(tmp_path)
    _install_specify_stub(tmp_path, monkeypatch)
    project = tmp_path / "meeting-booking"

    result = runner.invoke(
        app,
        ["new", str(project), "--catalog", str(catalog)],
    )

    assert result.exit_code == 0, result.output
    assert (project / ".git").is_dir()
    assert (project / ".devplane" / "project.yaml").is_file()
    assert (project / ".devplane" / "generated" / "resolved-manifest.yaml").is_file()
    assert (project / ".specify" / "integration.json").is_file()
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=project, check=True, capture_output=True, text=True
    ).stdout == ""
    assert "chore: bootstrap DevPlane project" in subprocess.run(
        ["git", "log", "-1", "--pretty=%s"], cwd=project, check=True, capture_output=True, text=True
    ).stdout
    validated = runner.invoke(app, ["validate", "--project", str(project)])
    assert validated.exit_code == 0, validated.output


def test_new_refuses_sensitive_environment_file_before_baseline(tmp_path: Path, monkeypatch) -> None:
    catalog = _catalog(tmp_path)
    _install_specify_stub(tmp_path, monkeypatch, create_env=True)
    project = tmp_path / "unsafe"

    result = runner.invoke(app, ["new", str(project), "--catalog", str(catalog)])

    assert result.exit_code == 1
    assert "sensitive environment file" in result.output
    assert subprocess.run(
        ["git", "log", "-1"], cwd=project, capture_output=True, check=False
    ).returncode != 0


def test_new_fails_before_creating_project_when_specify_is_missing(tmp_path: Path, monkeypatch) -> None:
    catalog = _catalog(tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    project = tmp_path / "missing-specify"

    result = runner.invoke(app, ["new", str(project), "--catalog", str(catalog)])

    assert result.exit_code == 1
    assert "required executable not found: specify" in result.output
    assert not project.exists()


def test_new_fails_before_creating_project_when_git_is_missing(tmp_path: Path, monkeypatch) -> None:
    catalog = _catalog(tmp_path)
    _install_specify_stub(tmp_path, monkeypatch)
    specify_bin = tmp_path / "bin"
    monkeypatch.setenv("PATH", str(specify_bin))
    project = tmp_path / "missing-git"

    result = runner.invoke(app, ["new", str(project), "--catalog", str(catalog)])

    assert result.exit_code == 1
    assert "required executable not found: git" in result.output
    assert not project.exists()


def test_new_refuses_nonempty_directory_before_checking_executables(tmp_path: Path, monkeypatch) -> None:
    catalog = _catalog(tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    project = tmp_path / "existing"
    project.mkdir()
    (project / "keep.txt").write_text("user data", encoding="utf-8")

    result = runner.invoke(app, ["new", str(project), "--catalog", str(catalog)])

    assert result.exit_code == 1
    assert "empty" in result.output
    assert (project / "keep.txt").read_text(encoding="utf-8") == "user data"
    assert sorted(path.name for path in project.iterdir()) == ["keep.txt"]
