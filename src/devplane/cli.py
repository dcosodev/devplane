from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml

from . import commands
from .context import build_context_bundle, write_context_bundle
from .core import DevPlaneError, build_resolved_manifest, sync_project
from .discovery import discover_repository, write_repository_profile
from .execution_plan import (
    build_execution_plan,
    compute_tasks_digest,
    load_execution_plan,
    write_execution_plan,
)
from .parallel import run_parallel_implementation
from .pipeline import (
    integrate_pipeline,
    load_pipeline_run,
    run_execution_pipeline,
    write_pipeline_run,
)
from .sdd import (
    FeatureState,
    execute_phase,
    load_feature_state,
    read_feature_request,
    write_feature_request,
    write_feature_state,
)
from .tasks import parse_tasks_markdown
from .worktrees import apply_worktree_cleanup, plan_worktree_cleanup

app = typer.Typer(
    help="Local-first control plane for GitHub Spec Kit and Hermes Agent",
    no_args_is_help=True,
)

HERMES_RULES_BEGIN = "<!-- BEGIN DEVPLANE MANAGED CONTRACT -->"
HERMES_RULES_END = "<!-- END DEVPLANE MANAGED CONTRACT -->"
SUPPORTED_SPECKIT_PREFIX = "0.14."
HERMES_PROJECT_RULES = f"""{HERMES_RULES_BEGIN}
# DevPlane runtime contract

This repository uses GitHub Spec Kit as its SDD workflow engine and Hermes as its agent runtime.

When executing a `speckit-<command>` skill:

1. Read `.devplane/generated/resolved-manifest.yaml` before taking action.
2. If it exists, read `.devplane/generated/context-<command>.md` before taking action.
3. Treat the manifest's write scopes and allowed commands as upper bounds.
4. Do not access network or paths outside the repository unless explicitly approved.
5. Run the repository's native validation before declaring implementation complete.

Generated files under `.devplane/generated/` must be regenerated with `devplane sync`, never edited manually.
{HERMES_RULES_END}
"""


def _project(value: Path) -> Path:
    return value.expanduser().resolve()


def _fail(exc: DevPlaneError) -> NoReturn:
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(1)


def _verify_contexts(project: Path, resolved: dict) -> None:
    expected_paths = {
        project / ".devplane" / "generated" / f"context-{command}.md"
        for command in resolved["spec"]["commands"]
    }
    for command in resolved["spec"]["commands"]:
        expected = build_context_bundle(project, command)
        context_path = project / ".devplane" / "generated" / f"context-{command}.md"
        if not context_path.is_file() or context_path.read_text(encoding="utf-8") != expected:
            raise DevPlaneError(f"generated context drift detected: {context_path}")
    generated_dir = project / ".devplane" / "generated"
    unexpected = sorted(path for path in generated_dir.glob("context-*.md") if path not in expected_paths)
    if unexpected:
        raise DevPlaneError(f"stale generated context detected: {unexpected[0]}")


def _ensure_hermes_rules(project: Path) -> None:
    rules_path = project / ".hermes.md"
    existing = rules_path.read_text(encoding="utf-8") if rules_path.exists() else ""
    if HERMES_RULES_BEGIN in existing and HERMES_RULES_END in existing:
        prefix, remainder = existing.split(HERMES_RULES_BEGIN, 1)
        _, suffix = remainder.split(HERMES_RULES_END, 1)
        updated = prefix.rstrip() + "\n\n" + HERMES_PROJECT_RULES.rstrip() + suffix
    elif existing:
        updated = existing.rstrip() + "\n\n" + HERMES_PROJECT_RULES
    else:
        updated = HERMES_PROJECT_RULES
    rules_path.write_text(updated.rstrip() + "\n", encoding="utf-8")


def _read_integration_state(project: Path) -> dict:
    state_path = project / ".specify" / "integration.json"
    if not state_path.is_file():
        raise DevPlaneError(f"Spec Kit integration state missing: {state_path}")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DevPlaneError(f"invalid Spec Kit integration state: {state_path}") from exc
    if "hermes" not in state.get("installed_integrations", []):
        raise DevPlaneError("Spec Kit Hermes integration is not installed")
    if state.get("default_integration") != "hermes":
        raise DevPlaneError("Spec Kit default integration is not hermes")
    version = state.get("version")
    if not isinstance(version, str) or not version.startswith(SUPPORTED_SPECKIT_PREFIX):
        raise DevPlaneError(
            f"unsupported Spec Kit integration version {version!r}; expected {SUPPORTED_SPECKIT_PREFIX}x"
        )
    return state


def _append_audit(project: Path, record: dict) -> None:
    git_dir = project / ".git"
    audit_dir = (
        git_dir / "devplane" / "audit"
        if git_dir.is_dir()
        else project / ".devplane" / "audit"
    )
    audit_dir.mkdir(parents=True, exist_ok=True)
    with (audit_dir / "runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _update_feature_status(project: Path, run_id: str, status: str) -> None:
    state_path = project / ".git" / "devplane" / "runs" / run_id / "feature-state.json"
    if not state_path.is_file():
        return
    state = load_feature_state(project, run_id)
    write_feature_state(project, replace(state, status=status))


def _require_executables(*names: str) -> None:
    for name in names:
        if shutil.which(name) is None:
            raise DevPlaneError(f"required executable not found: {name}")


def _initialize_project(project: Path, catalog: Path) -> None:
    _require_executables("specify")
    project.mkdir(parents=True, exist_ok=True)
    if not (catalog / "manifest.yaml").is_file():
        raise DevPlaneError(f"catalog manifest not found: {catalog / 'manifest.yaml'}")
    config_dir = project / ".devplane"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "apiVersion": "devplane.dev/v1",
        "kind": "AgentProject",
        "metadata": {"name": project.name},
        "spec": {
            "catalog": {"source": os.path.relpath(catalog, project)},
            "capabilities": [],
            "workflow": {"engine": "speckit", "specifyVersion": f"{SUPPORTED_SPECKIT_PREFIX}x"},
            "runtime": {"agent": "hermes"},
        },
    }
    (config_dir / "project.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    _ensure_hermes_rules(project)
    commands.run_checked(["specify", "init", ".", "--integration", "hermes", "--force"], project)


@app.command()
def init(
    project: Annotated[Path, typer.Argument(help="Project directory")],
    catalog: Annotated[Path, typer.Option("--catalog", help="Local capability catalog")],
) -> None:
    """Initialize Spec Kit with its native Hermes integration and DevPlane config."""
    project = _project(project)
    catalog = catalog.expanduser().resolve()
    try:
        _initialize_project(project, catalog)
    except (DevPlaneError, OSError) as exc:
        error = exc if isinstance(exc, DevPlaneError) else DevPlaneError(f"cannot initialize project: {exc}")
        _fail(error)
    typer.echo(f"initialized {project}")


@app.command("new")
def new_project(
    project: Annotated[Path, typer.Argument(help="New empty project directory")],
    catalog: Annotated[Path, typer.Option("--catalog", help="Local capability catalog")],
) -> None:
    """Create a governed greenfield project with a clean Git baseline."""
    project = _project(project)
    catalog = catalog.expanduser().resolve()
    try:
        _require_executables("specify", "git")
        if project.exists() and any(project.iterdir()):
            raise DevPlaneError(f"new project directory must be empty: {project}")
        project.mkdir(parents=True, exist_ok=True)
        commands.run_checked(["git", "init", "-b", "main"], project)
        _initialize_project(project, catalog)
        resolved = sync_project(project, check=False)
        for command in resolved["spec"]["commands"]:
            write_context_bundle(project, command)
        _read_integration_state(project)
        sensitive = sorted(
            path
            for path in project.rglob("*")
            if path.is_file()
            and ".git" not in path.relative_to(project).parts
            and (
                path.name == ".env"
                or (path.name.startswith(".env.") and path.name != ".env.example")
            )
        )
        if sensitive:
            raise DevPlaneError(
                f"sensitive environment file blocks baseline commit: {sensitive[0].relative_to(project)}"
            )
        commands.run_checked(["git", "add", "--all"], project)
        staged = commands.run_checked(["git", "diff", "--cached", "--name-only"], project).stdout
        if not staged:
            raise DevPlaneError("greenfield bootstrap produced no files to commit")
        commands.run_checked(["git", "commit", "-m", "chore: bootstrap DevPlane project"], project)
        base_commit = commands.run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip()
        if commands.run_checked(["git", "status", "--porcelain"], project).stdout:
            raise DevPlaneError("greenfield project is not clean after baseline commit")
    except (DevPlaneError, OSError) as exc:
        error = exc if isinstance(exc, DevPlaneError) else DevPlaneError(f"cannot create greenfield project: {exc}")
        _fail(error)
    typer.echo(
        json.dumps(
            {
                "project": str(project),
                "status": "ready_for_specification",
                "base_commit": base_commit,
                "manifest_digest": resolved["metadata"]["sourceHash"],
            },
            indent=2,
            sort_keys=True,
        )
    )


@app.command()
def sync(
    check: Annotated[bool, typer.Option("--check", help="Fail if generated output has drift")] = False,
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Resolve source manifests into deterministic generated state."""
    try:
        resolved = sync_project(_project(project), check=check)
        project_root = _project(project)
        if check:
            _verify_contexts(project_root, resolved)
        else:
            generated_dir = project_root / ".devplane" / "generated"
            expected_names = {f"context-{command}.md" for command in resolved["spec"]["commands"]}
            for stale in generated_dir.glob("context-*.md"):
                if stale.name not in expected_names:
                    stale.unlink()
            for command in resolved["spec"]["commands"]:
                write_context_bundle(project_root, command)
    except DevPlaneError as exc:
        _fail(exc)
    typer.echo("in sync" if check else f"synced {resolved['metadata']['sourceHash']}")


@app.command()
def validate(
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Validate project structure, manifests, and generated drift."""
    project = _project(project)
    if not (project / ".specify").is_dir():
        _fail(DevPlaneError(f"Spec Kit project directory missing: {project / '.specify'}"))
    if not (project / ".hermes" / "skills").is_dir():
        _fail(DevPlaneError("Spec Kit Hermes integration marker missing: .hermes/skills"))
    try:
        _read_integration_state(project)
        resolved = sync_project(project, check=True)
        _verify_contexts(project, resolved)
    except DevPlaneError as exc:
        _fail(exc)
    typer.echo("valid")


@app.command()
def inspect(
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Print the effective manifest."""
    try:
        resolved = build_resolved_manifest(_project(project))
    except DevPlaneError as exc:
        _fail(exc)
    typer.echo(yaml.safe_dump(resolved, sort_keys=False), nl=False)


@app.command()
def profile(
    approve: Annotated[bool, typer.Option("--approve", help="Record this exact detected profile in project.yaml")] = False,
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Detect repository stack and optionally approve the exact evidence digest."""
    project = _project(project)
    try:
        if not (project / ".git").is_dir():
            raise DevPlaneError("repository profiling requires a Git project")
        discovered = discover_repository(project)
        profile_path = write_repository_profile(project, discovered)
        if approve:
            config_path = project / ".devplane" / "project.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if not isinstance(config, dict) or not isinstance(config.get("spec"), dict):
                raise DevPlaneError("invalid AgentProject while approving repository profile")
            approved = {
                key: value
                for key, value in discovered.items()
                if key not in {"apiVersion", "kind"}
            }
            approved["approved"] = True
            config["spec"]["repositoryProfile"] = approved
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            resolved = sync_project(project, check=False)
            generated_dir = project / ".devplane" / "generated"
            expected_names = {f"context-{command}.md" for command in resolved["spec"]["commands"]}
            for stale in generated_dir.glob("context-*.md"):
                if stale.name not in expected_names:
                    stale.unlink()
            for command in resolved["spec"]["commands"]:
                write_context_bundle(project, command)
            _append_audit(
                project,
                {
                    "event": "repository.profile.approved",
                    "profile_digest": discovered["digest"],
                    "manifest_digest": resolved["metadata"]["sourceHash"],
                    "recorded_at": datetime.now(UTC).isoformat(),
                },
            )
    except (DevPlaneError, OSError, UnicodeError, yaml.YAMLError) as exc:
        error = exc if isinstance(exc, DevPlaneError) else DevPlaneError(f"cannot profile repository: {exc}")
        _fail(error)
    payload = {**discovered, "approved": approve, "profile_path": str(profile_path)}
    typer.echo(yaml.safe_dump(payload, sort_keys=False), nl=False)


@app.command()
def activate(
    capability: Annotated[str, typer.Argument(help="Catalog capability ID, preferably pinned with @version")],
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Explicitly activate one catalog capability and regenerate governed context."""
    project = _project(project)
    config_path = project / ".devplane" / "project.yaml"
    original: bytes | None = None
    try:
        original = config_path.read_bytes()
        config = yaml.safe_load(original)
        if not isinstance(config, dict) or not isinstance(config.get("spec"), dict):
            raise DevPlaneError("invalid AgentProject while activating capability")
        active = config["spec"].get("capabilities", [])
        if not isinstance(active, list) or not all(isinstance(item, str) for item in active):
            raise DevPlaneError("spec.capabilities must be a list of strings")
        if capability not in active:
            active.append(capability)
        config["spec"]["capabilities"] = active
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        resolved = sync_project(project, check=False)
        generated_dir = project / ".devplane" / "generated"
        expected_names = {f"context-{command}.md" for command in resolved["spec"]["commands"]}
        for stale in generated_dir.glob("context-*.md"):
            if stale.name not in expected_names:
                stale.unlink()
        for command in resolved["spec"]["commands"]:
            write_context_bundle(project, command)
    except (DevPlaneError, OSError, UnicodeError, yaml.YAMLError) as exc:
        if original is not None:
            config_path.write_bytes(original)
        error = exc if isinstance(exc, DevPlaneError) else DevPlaneError(f"cannot activate capability: {exc}")
        _fail(error)
    _append_audit(
        project,
        {
            "event": "capability.activated",
            "capability": capability,
            "manifest_digest": resolved["metadata"]["sourceHash"],
            "recorded_at": datetime.now(UTC).isoformat(),
        },
    )
    typer.echo(json.dumps({"capability": capability, "manifest_digest": resolved["metadata"]["sourceHash"]}, indent=2))


@app.command("context")
def context_command(
    command: Annotated[str, typer.Argument(help="Logical workflow command")],
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Explain and render the exact context selected for a command."""
    try:
        typer.echo(build_context_bundle(_project(project), command), nl=False)
    except DevPlaneError as exc:
        _fail(exc)


@app.command()
def run(
    spec: Annotated[str, typer.Argument(help="Feature description passed to the Spec Kit workflow")],
    stop_after: Annotated[str | None, typer.Option("--stop-after", help="Governed cutoff; currently only 'tasks'")] = None,
    run_id: Annotated[str | None, typer.Option("--run-id", help="Stable DevPlane feature run ID")] = None,
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Run the native workflow or start the governed phase-by-phase flow."""
    project = _project(project)
    try:
        resolved = sync_project(project, check=True)
        _verify_contexts(project, resolved)
        if stop_after is not None:
            if stop_after != "tasks":
                raise DevPlaneError("--stop-after currently supports only 'tasks'")
            effective_run_id = run_id or f"feature-{uuid.uuid4().hex[:12]}"
            state_path = project / ".git" / "devplane" / "runs" / effective_run_id / "feature-state.json"
            if state_path.exists():
                raise DevPlaneError(f"feature run already exists: {effective_run_id}")
            spec_digest = f"sha256:{hashlib.sha256(spec.encode('utf-8')).hexdigest()}"
            write_feature_request(project, effective_run_id, spec)
            phase_result = execute_phase(
                project,
                effective_run_id,
                "specify",
                spec,
                command_runner=commands.run_checked,
            )
            state = FeatureState(
                run_id=effective_run_id,
                status="spec_pending_approval",
                manifest_digest=resolved["metadata"]["sourceHash"],
                spec_digest=spec_digest,
                external_runs={"specify": str(phase_result["run_id"])},
            )
            write_feature_state(project, state)
            _append_audit(
                project,
                {
                    "event": "sdd.phase.completed",
                    "phase": "specify",
                    "run_id": effective_run_id,
                    "external_run_id": phase_result["run_id"],
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "manifest_digest": state.manifest_digest,
                    "spec_digest": state.spec_digest,
                },
            )
            typer.echo(json.dumps(state.to_mapping(), indent=2, sort_keys=True))
            return
        result = commands.run_checked(
            [
                "specify", "workflow", "run", "speckit",
                "--input", f"spec={spec}",
                "--input", "integration=hermes",
                "--json",
            ],
            project,
        )
        workflow_result = json.loads(result.stdout or "{}")
    except (DevPlaneError, json.JSONDecodeError) as exc:
        error = exc if isinstance(exc, DevPlaneError) else DevPlaneError(f"invalid Spec Kit JSON output: {exc}")
        _fail(error)

    record = {
        **workflow_result,
        "recorded_at": datetime.now(UTC).isoformat(),
        "manifest_digest": resolved["metadata"]["sourceHash"],
        "spec_digest": f"sha256:{hashlib.sha256(spec.encode('utf-8')).hexdigest()}",
        "workflow": "speckit",
        "integration": "hermes",
    }
    _append_audit(project, record)
    typer.echo(json.dumps(workflow_result, indent=2, sort_keys=True))


@app.command()
def approve(
    run_id: Annotated[str, typer.Argument(help="DevPlane feature run ID")],
    artifact: Annotated[str, typer.Argument(help="Artifact gate: spec, plan, or tasks")],
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Approve one SDD artifact and advance exactly one governed phase."""
    project = _project(project)
    transitions = {
        "spec": ("spec_pending_approval", "plan", "plan_pending_approval"),
        "plan": ("plan_pending_approval", "tasks", "tasks_pending_approval"),
        "tasks": ("tasks_pending_approval", None, "ready_to_checkpoint"),
    }
    if artifact not in transitions:
        _fail(DevPlaneError("artifact must be one of: spec, plan, tasks"))
    try:
        resolved = sync_project(project, check=True)
        _verify_contexts(project, resolved)
        state = load_feature_state(project, run_id)
        expected, next_phase, next_status = transitions[artifact]
        if state.status != expected:
            raise DevPlaneError(
                f"cannot approve {artifact} while run is {state.status}; expected {expected}"
            )
        if state.manifest_digest != resolved["metadata"]["sourceHash"]:
            raise DevPlaneError("feature run manifest drift detected")
        external_runs = dict(state.external_runs)
        external_run_id: str | None = None
        if next_phase is not None:
            spec = read_feature_request(project, run_id, state.spec_digest)
            phase_result = execute_phase(
                project,
                run_id,
                next_phase,
                spec,
                command_runner=commands.run_checked,
            )
            external_run_id = str(phase_result["run_id"])
            external_runs[next_phase] = external_run_id
        updated = FeatureState(
            run_id=state.run_id,
            status=next_status,
            manifest_digest=state.manifest_digest,
            spec_digest=state.spec_digest,
            external_runs=external_runs,
        )
        write_feature_state(project, updated)
    except DevPlaneError as exc:
        _fail(exc)
    _append_audit(
        project,
        {
            "event": "sdd.artifact.approved",
            "artifact": artifact,
            "run_id": run_id,
            "next_phase": next_phase,
            "external_run_id": external_run_id,
            "status": updated.status,
            "recorded_at": datetime.now(UTC).isoformat(),
            "manifest_digest": updated.manifest_digest,
        },
    )
    typer.echo(json.dumps(updated.to_mapping(), indent=2, sort_keys=True))


@app.command()
def checkpoint(
    run_id: Annotated[str, typer.Argument(help="Approved DevPlane feature run ID")],
    tasks_file: Annotated[Path, typer.Option("--tasks-file", help="Approved feature tasks.md")],
    validation: Annotated[list[str] | None, typer.Option("--validation", help="Exact validation command; repeat as needed")] = None,
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Commit only approved Spec Kit artifacts and create their execution plan."""
    project = _project(project)
    try:
        resolved = sync_project(project, check=True)
        _verify_contexts(project, resolved)
        state = load_feature_state(project, run_id)
        if state.status != "ready_to_checkpoint":
            raise DevPlaneError(f"feature run is not ready to checkpoint: {state.status}")
        if state.manifest_digest != resolved["metadata"]["sourceHash"]:
            raise DevPlaneError("feature run manifest drift detected")
        candidate = tasks_file.expanduser()
        if not candidate.is_absolute():
            candidate = project / candidate
        source = candidate.resolve()
        if not source.is_relative_to(project) or source.name != "tasks.md" or not source.is_file():
            raise DevPlaneError("tasks file must be a tasks.md inside the project")
        feature_dir = source.parent
        for required in ("spec.md", "plan.md", "tasks.md"):
            if not (feature_dir / required).is_file():
                raise DevPlaneError(f"approved feature artifact missing: {feature_dir / required}")
        staged = commands.run_checked(["git", "diff", "--cached", "--name-only"], project).stdout
        if staged:
            raise DevPlaneError("project has pre-staged changes; checkpoint refuses ambiguous scope")
        feature_relative = feature_dir.relative_to(project).as_posix()
        changed = commands.run_checked(
            ["git", "status", "--porcelain", "--untracked-files=all"], project
        ).stdout.splitlines()
        for line in changed:
            relative = line[3:].split(" -> ")[-1]
            if relative != feature_relative and not relative.startswith(feature_relative + "/"):
                raise DevPlaneError(f"unrelated working tree change blocks checkpoint: {relative}")
        phases = parse_tasks_markdown(source.read_text(encoding="utf-8"))
        current = commands.run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip()
        # Validate all assignment boundaries before creating the checkpoint commit.
        build_execution_plan(
            phases,
            run_id=run_id,
            source_tasks=source.relative_to(project).as_posix(),
            manifest_digest=state.manifest_digest,
            base_commit=current,
            validation_commands=validation or [],
        )
        commands.run_checked(["git", "add", "--", feature_relative], project)
        commands.run_checked(
            ["git", "commit", "-m", f"docs: approve {run_id} implementation plan"],
            project,
        )
        base_commit = commands.run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip()
        plan = build_execution_plan(
            phases,
            run_id=run_id,
            source_tasks=source.relative_to(project).as_posix(),
            manifest_digest=state.manifest_digest,
            base_commit=base_commit,
            validation_commands=validation or [],
        )
        output = project / ".git" / "devplane" / "runs" / run_id / "execution-plan.yaml"
        write_execution_plan(plan, output)
        updated = FeatureState(
            run_id=state.run_id,
            status="ready_to_implement",
            manifest_digest=state.manifest_digest,
            spec_digest=state.spec_digest,
            external_runs=state.external_runs,
        )
        write_feature_state(project, updated)
    except (DevPlaneError, OSError, UnicodeError, ValueError) as exc:
        error = exc if isinstance(exc, DevPlaneError) else DevPlaneError(f"cannot checkpoint feature: {exc}")
        _fail(error)
    _append_audit(
        project,
        {
            "event": "sdd.checkpoint.created",
            "run_id": run_id,
            "recorded_at": datetime.now(UTC).isoformat(),
            "manifest_digest": state.manifest_digest,
            "base_commit": plan.base_commit,
            "tasks_digest": plan.tasks_digest,
        },
    )
    typer.echo(
        json.dumps(
            {"run_id": run_id, "status": updated.status, "base_commit": plan.base_commit, "execution_plan": str(output)},
            indent=2,
            sort_keys=True,
        )
    )


@app.command()
def plan_execution(
    tasks_file: Annotated[Path, typer.Option("--tasks-file", help="Approved Spec Kit tasks.md inside the project")],
    validation: Annotated[list[str] | None, typer.Option("--validation", help="Exact validation command; repeat for multiple commands")] = None,
    run_id: Annotated[str | None, typer.Option("--run-id", help="Stable safe identifier; generated when omitted")] = None,
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Create and persist a governed execution plan from approved tasks."""
    project = _project(project)
    try:
        resolved = sync_project(project, check=True)
        _verify_contexts(project, resolved)
        candidate = tasks_file.expanduser()
        if not candidate.is_absolute():
            candidate = project / candidate
        source = candidate.resolve()
        if not source.is_relative_to(project) or not source.is_file():
            raise DevPlaneError(f"tasks file must be a file inside the project: {tasks_file}")
        status = commands.run_checked(["git", "status", "--porcelain"], project).stdout
        if status:
            raise DevPlaneError("project must be clean before creating an execution plan")
        base_commit = commands.run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", base_commit):
            raise DevPlaneError("git rev-parse HEAD did not return a valid object id")
        phases = parse_tasks_markdown(source.read_text(encoding="utf-8"))
        effective_run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        plan = build_execution_plan(
            phases,
            run_id=effective_run_id,
            source_tasks=source.relative_to(project).as_posix(),
            manifest_digest=resolved["metadata"]["sourceHash"],
            base_commit=base_commit,
            validation_commands=validation or [],
        )
        output = project / ".git" / "devplane" / "runs" / effective_run_id / "execution-plan.yaml"
        write_execution_plan(plan, output)
    except (DevPlaneError, OSError, UnicodeError, ValueError) as exc:
        error = exc if isinstance(exc, DevPlaneError) else DevPlaneError(f"cannot create execution plan: {exc}")
        _fail(error)
    typer.echo(json.dumps({"run_id": plan.run_id, "execution_plan": str(output)}, indent=2, sort_keys=True))


@app.command()
def implement(
    tasks_file: Annotated[Path | None, typer.Option("--tasks-file", help="Legacy Spec Kit tasks.md inside the project")] = None,
    execution_plan: Annotated[Path | None, typer.Option("--execution-plan", help="Governed execution-plan.yaml created by plan-execution")] = None,
    parallel: Annotated[bool, typer.Option("--parallel", help="Use isolated MiniMax sessions")] = False,
    max_agents: Annotated[int, typer.Option("--max-agents", min=1, max=8)] = 3,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the execution plan without launching agents")] = False,
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Implement Spec Kit tasks with bounded parallel MiniMax sessions."""
    if not parallel:
        _fail(DevPlaneError("implement currently requires --parallel"))
    if (tasks_file is None) == (execution_plan is None):
        _fail(DevPlaneError("provide exactly one of --tasks-file or --execution-plan"))
    project = _project(project)
    try:
        resolved = sync_project(project, check=True)
        _verify_contexts(project, resolved)
        if execution_plan is not None:
            candidate = execution_plan.expanduser()
            if not candidate.is_absolute():
                candidate = project / candidate
            plan_path = candidate.resolve()
            state_root = (project / ".git" / "devplane" / "runs").resolve()
            if not plan_path.is_relative_to(state_root) or not plan_path.is_file():
                raise DevPlaneError("execution plan must be inside local .git/devplane/runs state")
            plan = load_execution_plan(plan_path)
            if plan.manifest_digest != resolved["metadata"]["sourceHash"]:
                raise DevPlaneError("execution plan manifest drift detected")
            source = (project / plan.source_tasks).resolve()
            if not source.is_relative_to(project) or not source.is_file():
                raise DevPlaneError("execution plan tasks source is unavailable")
            phases = parse_tasks_markdown(source.read_text(encoding="utf-8"))
            if compute_tasks_digest(phases) != plan.tasks_digest:
                raise DevPlaneError("execution plan tasks drift detected")
            if dry_run:
                typer.echo(
                    json.dumps(
                        {
                            "run_id": plan.run_id,
                            "dry_run": True,
                            "assignments": [asdict(item) for item in plan.assignments],
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return
            pipeline_result = run_execution_pipeline(project, plan, max_agents=max_agents)
            result_path = plan_path.parent / "result.json"
            write_pipeline_run(pipeline_result, result_path)
            _update_feature_status(
                project,
                plan.run_id,
                "ready_to_integrate" if pipeline_result.status == "completed" else "implementation_failed",
            )
            for assignment in pipeline_result.assignments:
                _append_audit(
                    project,
                    {
                        "event": "pipeline.assignment",
                        "run_id": pipeline_result.run_id,
                        "recorded_at": datetime.now(UTC).isoformat(),
                        "manifest_digest": resolved["metadata"]["sourceHash"],
                        "provider": "minimax-oauth",
                        "model": "MiniMax-M3",
                        **asdict(assignment),
                    },
                )
            typer.echo(json.dumps(asdict(pipeline_result), indent=2, sort_keys=True))
            if pipeline_result.status != "completed":
                raise typer.Exit(1)
            return
        assert tasks_file is not None
        result = run_parallel_implementation(
            project,
            tasks_file,
            manifest_digest=resolved["metadata"]["sourceHash"],
            max_agents=max_agents,
            dry_run=dry_run,
        )
    except DevPlaneError as exc:
        _fail(exc)

    payload = {
        "run_id": result.run_id,
        "dry_run": result.dry_run,
        "phases": [asdict(phase) for phase in result.phases],
    }
    if not dry_run:
        for phase in result.phases:
            _append_audit(
                project,
                {
                    "event": "parallel.implement.phase",
                    "run_id": result.run_id,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "manifest_digest": resolved["metadata"]["sourceHash"],
                    "provider": "minimax-oauth",
                    "model": "MiniMax-M3",
                    **asdict(phase),
                },
            )
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    if any(phase.status in {"failed", "blocked"} for phase in result.phases):
        raise typer.Exit(1)


@app.command("run-status")
def run_status(
    run_id: Annotated[str, typer.Argument(help="DevPlane execution run ID")],
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Show the persisted local status of an execution run."""
    project = _project(project)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_id):
        _fail(DevPlaneError("run_id must be a safe identifier"))
    state_dir = project / ".git" / "devplane" / "runs" / run_id
    try:
        feature_path = state_dir / "feature-state.json"
        feature = load_feature_state(project, run_id) if feature_path.is_file() else None
        plan_path = state_dir / "execution-plan.yaml"
        plan = load_execution_plan(plan_path) if plan_path.is_file() else None
        result_path = state_dir / "result.json"
        result = load_pipeline_run(result_path) if result_path.is_file() else None
        if feature is None and plan is None:
            raise DevPlaneError(f"run state does not exist: {run_id}")
    except DevPlaneError as exc:
        _fail(exc)
    status = feature.status if feature is not None else result.status if result is not None else "planned"
    assignments = (
        result.assignments
        if result is not None
        else plan.assignments
        if plan is not None
        else ()
    )
    typer.echo(
        json.dumps(
            {
                "run_id": run_id,
                "status": status,
                "base_commit": plan.base_commit if plan is not None else None,
                "final_commit": result.final_commit if result else None,
                "external_runs": feature.external_runs if feature is not None else {},
                "assignments": [asdict(item) for item in assignments],
            },
            indent=2,
            sort_keys=True,
        )
    )


@app.command("integrate")
def integrate_command(
    run_id: Annotated[str, typer.Argument(help="Completed DevPlane execution run ID")],
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Explicitly fast-forward a completed, reviewed pipeline into the project."""
    project = _project(project)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_id):
        _fail(DevPlaneError("run_id must be a safe identifier"))
    state_dir = project / ".git" / "devplane" / "runs" / run_id
    try:
        resolved = sync_project(project, check=True)
        _verify_contexts(project, resolved)
        plan = load_execution_plan(state_dir / "execution-plan.yaml")
        result = load_pipeline_run(state_dir / "result.json")
        if plan.manifest_digest != resolved["metadata"]["sourceHash"]:
            raise DevPlaneError("cannot integrate after manifest drift")
        final_commit = integrate_pipeline(project, plan, result)
        _update_feature_status(project, run_id, "integrated")
    except DevPlaneError as exc:
        _fail(exc)
    _append_audit(
        project,
        {
            "event": "pipeline.integrated",
            "run_id": run_id,
            "recorded_at": datetime.now(UTC).isoformat(),
            "manifest_digest": resolved["metadata"]["sourceHash"],
            "base_commit": plan.base_commit,
            "final_commit": final_commit,
        },
    )
    typer.echo(json.dumps({"run_id": run_id, "status": "integrated", "final_commit": final_commit}, indent=2))


@app.command()
def retry(
    run_id: Annotated[str, typer.Argument(help="Failed DevPlane execution run ID")],
    max_agents: Annotated[int, typer.Option("--max-agents", min=1, max=8)] = 3,
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Retry failed and affected assignments while reusing valid completed commits."""
    project = _project(project)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_id):
        _fail(DevPlaneError("run_id must be a safe identifier"))
    state_dir = project / ".git" / "devplane" / "runs" / run_id
    try:
        resolved = sync_project(project, check=True)
        _verify_contexts(project, resolved)
        plan = load_execution_plan(state_dir / "execution-plan.yaml")
        prior = load_pipeline_run(state_dir / "result.json")
        if prior.status != "failed":
            raise DevPlaneError("retry requires a failed pipeline result")
        if plan.manifest_digest != resolved["metadata"]["sourceHash"]:
            raise DevPlaneError("cannot retry after manifest drift")
        source = (project / plan.source_tasks).resolve()
        if not source.is_relative_to(project) or not source.is_file():
            raise DevPlaneError("execution plan tasks source is unavailable")
        phases = parse_tasks_markdown(source.read_text(encoding="utf-8"))
        if compute_tasks_digest(phases) != plan.tasks_digest:
            raise DevPlaneError("cannot retry after tasks drift")
        retried = run_execution_pipeline(
            project,
            plan,
            max_agents=max_agents,
            prior_result=prior,
        )
        write_pipeline_run(retried, state_dir / "result.json")
        _update_feature_status(
            project,
            run_id,
            "ready_to_integrate" if retried.status == "completed" else "implementation_failed",
        )
    except (DevPlaneError, OSError, UnicodeError) as exc:
        error = exc if isinstance(exc, DevPlaneError) else DevPlaneError(f"cannot retry pipeline: {exc}")
        _fail(error)
    _append_audit(
        project,
        {
            "event": "pipeline.retried",
            "run_id": run_id,
            "status": retried.status,
            "recorded_at": datetime.now(UTC).isoformat(),
            "manifest_digest": resolved["metadata"]["sourceHash"],
            "reused_assignments": [
                item.assignment_id
                for item in prior.assignments
                if item.status == "completed"
                and any(
                    current.assignment_id == item.assignment_id
                    and current.commit == item.commit
                    for current in retried.assignments
                )
            ],
        },
    )
    typer.echo(json.dumps(asdict(retried), indent=2, sort_keys=True))


@app.command()
def cleanup(
    run_id: Annotated[str, typer.Argument(help="DevPlane execution run ID")],
    apply: Annotated[bool, typer.Option("--apply", help="Apply the inspected cleanup; default is dry-run")] = False,
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Remove only worktrees and branches proven integrated into HEAD."""
    project = _project(project)
    try:
        cleanup_plan = plan_worktree_cleanup(project, run_id)
        if apply:
            apply_worktree_cleanup(project, cleanup_plan)
    except DevPlaneError as exc:
        _fail(exc)
    if apply:
        _append_audit(
            project,
            {
                "event": "pipeline.cleanup.applied",
                "run_id": run_id,
                "removed_worktrees": len(cleanup_plan.worktrees),
                "removed_branches": len(cleanup_plan.safe_branches),
                "preserved_branches": list(cleanup_plan.preserved_branches),
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )
    typer.echo(
        json.dumps(
            {
                **asdict(cleanup_plan),
                "applied": apply,
            },
            indent=2,
            sort_keys=True,
        )
    )


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="Paused Spec Kit workflow run ID")],
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Resume a paused Spec Kit workflow, including interactive review gates."""
    project = _project(project)
    try:
        result = commands.run_checked(
            ["specify", "workflow", "resume", run_id, "--json"],
            project,
        )
        workflow_result = json.loads(result.stdout or "{}")
    except (DevPlaneError, json.JSONDecodeError) as exc:
        error = exc if isinstance(exc, DevPlaneError) else DevPlaneError(f"invalid Spec Kit JSON output: {exc}")
        _fail(error)
    _append_audit(
        project,
        {
            **workflow_result,
            "recorded_at": datetime.now(UTC).isoformat(),
            "operation": "resume",
            "run_id": run_id,
            "workflow": "speckit",
            "integration": "hermes",
        },
    )
    typer.echo(json.dumps(workflow_result, indent=2, sort_keys=True))
