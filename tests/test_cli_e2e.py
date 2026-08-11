from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from devplane.cli import app

runner = CliRunner()


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_governed_cli_e2e_from_greenfield_to_integrated_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    catalog = tmp_path / "catalog"
    (catalog / "capabilities").mkdir(parents=True)
    (catalog / "manifest.yaml").write_text(
        "apiVersion: devplane.dev/v1\n"
        "kind: CapabilityCatalog\n"
        "metadata: {name: e2e}\n"
        "spec: {capabilities: []}\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "specify",
        """#!/usr/bin/env python3
import json
import pathlib
import subprocess
import sys

cwd = pathlib.Path.cwd()
args = sys.argv[1:]
if args and args[0] == "init":
    (cwd / ".specify").mkdir(exist_ok=True)
    (cwd / ".hermes" / "skills").mkdir(parents=True, exist_ok=True)
    (cwd / ".specify" / "integration.json").write_text(json.dumps({
        "version": "0.14.0", "installed_integrations": ["hermes"], "default_integration": "hermes"
    }))
    raise SystemExit(0)
if args[:2] == ["workflow", "run"]:
    workflow = pathlib.Path(args[2]).read_text()
    feature = cwd / "specs" / "001-demo"
    feature.mkdir(parents=True, exist_ok=True)
    if "/speckit-specify" in workflow:
        subprocess.run(["git", "checkout", "-b", "001-demo"], cwd=cwd, check=True, capture_output=True)
        (feature / "spec.md").write_text("# Demo specification\\n")
        phase = "specify"
    elif "/speckit-plan" in workflow:
        (feature / "plan.md").write_text("# Demo plan\\n")
        phase = "plan"
    elif "/speckit-tasks" in workflow:
        (feature / "tasks.md").write_text(
            "## Phase 1: Setup\\n- [ ] T001 Create setup/generated.txt\\n\\n"
            "## Phase 2: Foundational\\n- [ ] T002 Create foundation/generated.txt\\n\\n"
            "## Phase 3: User Story 1\\n- [ ] T003 [US1] Create alpha/generated.txt\\n\\n"
            "## Phase 4: User Story 2\\n- [ ] T004 [US2] Create beta/generated.txt\\n\\n"
            "## Phase 5: Polish\\n- [ ] T005 Create polish/generated.txt\\n"
        )
        phase = "tasks"
    else:
        raise SystemExit("unknown workflow")
    print(json.dumps({"run_id": f"external-{phase}", "status": "completed"}))
    raise SystemExit(0)
print("unsupported specify invocation", file=sys.stderr)
raise SystemExit(2)
""",
    )
    _executable(
        bin_dir / "hermes",
        """#!/usr/bin/env python3
import pathlib
import subprocess
import sys

if "--version" in sys.argv:
    print("0.19.0")
    raise SystemExit(0)
prompt = sys.argv[sys.argv.index("--query") + 1]
assignment = prompt.split("Asignación: ", 1)[1].splitlines()[0]
scope_block = prompt.split("Paths de escritura permitidos:\\n", 1)[1].split("\\n\\nValidación", 1)[0]
for line in scope_block.splitlines():
    if not line.startswith("- "):
        continue
    relative = line[2:]
    target = pathlib.Path.cwd() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(assignment + "\\n")
    subprocess.run(["git", "add", relative], check=True)
subprocess.run(["git", "commit", "-m", f"test: {assignment}"], check=True, capture_output=True)
print(f"session_id: e2e-{assignment}")
""",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    project = tmp_path / "demo"

    created = runner.invoke(
        app,
        ["new", str(project), "--catalog", str(catalog)],
    )
    assert created.exit_code == 0, created.output

    started = runner.invoke(
        app,
        [
            "run",
            "Build demo",
            "--stop-after",
            "tasks",
            "--run-id",
            "e2e-run",
            "--project",
            str(project),
        ],
    )
    assert started.exit_code == 0, started.output
    for phase in ("spec", "plan", "tasks"):
        approved = runner.invoke(
            app,
            ["approve", "e2e-run", phase, "--project", str(project)],
        )
        assert approved.exit_code == 0, approved.output

    checkpoint = runner.invoke(
        app,
        [
            "checkpoint",
            "e2e-run",
            "--tasks-file",
            str(project / "specs" / "001-demo" / "tasks.md"),
            "--validation",
            "python3 -c pass",
            "--project",
            str(project),
        ],
    )
    assert checkpoint.exit_code == 0, checkpoint.output
    plan_path = project / ".git" / "devplane" / "runs" / "e2e-run" / "execution-plan.yaml"

    implemented = runner.invoke(
        app,
        [
            "implement",
            "--parallel",
            "--max-agents",
            "2",
            "--execution-plan",
            str(plan_path),
            "--project",
            str(project),
        ],
    )
    assert implemented.exit_code == 0, implemented.output

    integrated = runner.invoke(app, ["integrate", "e2e-run", "--project", str(project)])
    assert integrated.exit_code == 0, integrated.output
    status = runner.invoke(app, ["run-status", "e2e-run", "--project", str(project)])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["status"] == "integrated"

    expected = {
        "setup/generated.txt",
        "foundation/generated.txt",
        "alpha/generated.txt",
        "beta/generated.txt",
        "polish/generated.txt",
    }
    tracked = set(
        subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    assert expected.issubset(tracked)

    cleanup = runner.invoke(app, ["cleanup", "e2e-run", "--apply", "--project", str(project)])
    assert cleanup.exit_code == 0, cleanup.output
    assert not (project.parent / ".devplane-worktrees" / project.name / "e2e-run").exists()
    assert subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
