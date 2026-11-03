from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml

from . import commands
from .agent_runtime import list_adapters, runtime_from_manifest
from .backlog import (
    STATUSES,
    Story,
    load_backlog,
    plan_batches,
    ready_stories,
    set_status,
)
from .backlog_run import (
    BACKLOG_SOURCE,
    build_backlog_plan,
    claim_stories,
    mark_ready_for_review,
    verify_backlog_source,
)
from .context import build_context_bundle, write_context_bundle
from .core import DEFAULT_ADAPTER, DevPlaneError, build_resolved_manifest, sync_project
from .discovery import discover_repository, write_repository_profile
from .execution_plan import (
    build_execution_plan,
    compute_tasks_digest,
    load_execution_plan,
    write_execution_plan,
)
from .guard import decide, load_policy
from .parallel import run_parallel_implementation
from .pipeline import (
    integrate_pipeline,
    load_pipeline_run,
    run_execution_pipeline,
    write_pipeline_run,
)
from .sdd import (
    SPECKIT_INTEGRATIONS,
    FeatureState,
    PhaseSettings,
    execute_phase,
    load_feature_state,
    phase_settings,
    read_feature_request,
    write_feature_request,
    write_feature_state,
)
from .tasks import parse_tasks_markdown
from .worktrees import apply_worktree_cleanup, plan_worktree_cleanup

app = typer.Typer(
    help="Local-first organizational catalog and multi-agent development control plane",
    no_args_is_help=True,
)

RULES_BEGIN = "<!-- BEGIN DEVPLANE MANAGED CONTRACT -->"
RULES_END = "<!-- END DEVPLANE MANAGED CONTRACT -->"
SUPPORTED_SPECKIT_PREFIX = "0.14."
SPECKIT_RUN_STATE = ("/.specify/feature.json", "/.specify/workflows/runs/")
PROJECT_RULES = f"""{RULES_BEGIN}
# DevPlane runtime contract

This repository uses GitHub Spec Kit as its SDD workflow engine. The implementation runtime is configured separately in `.devplane/project.yaml`.

When executing a Spec Kit command such as `speckit-plan`:

1. Read `.devplane/generated/resolved-manifest.yaml` before taking action.
2. If it exists, read `.devplane/generated/context-<command>.md` before taking action.
3. Treat the manifest's write scopes and allowed commands as upper bounds.
4. Do not access network or paths outside the repository unless explicitly approved.
5. Run the repository's native validation before declaring implementation complete.

Generated files under `.devplane/generated/` must be regenerated with `devplane sync`, never edited manually.
{RULES_END}
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


def _ensure_project_rules(project: Path, integration: str) -> None:
    rules_path = project / SPECKIT_INTEGRATIONS[integration].rules_file
    existing = rules_path.read_text(encoding="utf-8") if rules_path.exists() else ""
    if RULES_BEGIN in existing and RULES_END in existing:
        prefix, remainder = existing.split(RULES_BEGIN, 1)
        _, suffix = remainder.split(RULES_END, 1)
        updated = prefix.rstrip() + "\n\n" + PROJECT_RULES.rstrip() + suffix
    elif existing:
        updated = existing.rstrip() + "\n\n" + PROJECT_RULES
    else:
        updated = PROJECT_RULES
    rules_path.write_text(updated.rstrip() + "\n", encoding="utf-8")


def _read_integration_state(project: Path) -> dict:
    state_path = project / ".specify" / "integration.json"
    if not state_path.is_file():
        raise DevPlaneError(f"Spec Kit integration state missing: {state_path}")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DevPlaneError(f"invalid Spec Kit integration state: {state_path}") from exc
    integration = state.get("default_integration")
    if integration not in SPECKIT_INTEGRATIONS:
        raise DevPlaneError(f"unsupported Spec Kit default integration: {integration!r}")
    if integration not in state.get("installed_integrations", []):
        raise DevPlaneError(f"Spec Kit {integration} integration is not installed")
    version = state.get("version")
    if not isinstance(version, str) or not version.startswith(SUPPORTED_SPECKIT_PREFIX):
        raise DevPlaneError(
            f"unsupported Spec Kit integration version {version!r}; expected {SUPPORTED_SPECKIT_PREFIX}x"
        )
    return state


def _exclude_speckit_run_state(project: Path) -> None:
    """Keep Spec Kit's per-run state out of Git status, without touching a tracked file."""
    info = project / ".git" / "info"
    if not (project / ".git").is_dir():
        return
    info.mkdir(exist_ok=True)
    exclude = info / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    missing = [entry for entry in SPECKIT_RUN_STATE if entry not in existing.splitlines()]
    if missing:
        prefix = existing if not existing or existing.endswith("\n") else f"{existing}\n"
        exclude.write_text(prefix + "".join(f"{entry}\n" for entry in missing), encoding="utf-8")


def _phase_settings(project: Path, resolved: dict) -> PhaseSettings:
    integration = _read_integration_state(project)["default_integration"]
    runtime = runtime_from_manifest(resolved) if resolved["spec"].get("runtime") else None
    return phase_settings(integration, runtime)


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


def _validate_modes(workflow: str, runtime: str, integration: str | None = None) -> None:
    if workflow not in {"none", "speckit"}:
        raise DevPlaneError(f"unsupported workflow engine: {workflow}")
    if runtime != "none" and runtime not in list_adapters():
        raise DevPlaneError(f"unsupported agent adapter: {runtime}")
    if integration is not None and integration not in SPECKIT_INTEGRATIONS:
        raise DevPlaneError(f"unsupported Spec Kit integration: {integration}")


def _initialize_project(
    project: Path,
    catalog: Path,
    workflow: str = "speckit",
    runtime: str = DEFAULT_ADAPTER,
    integration: str | None = None,
) -> None:
    _validate_modes(workflow, runtime, integration)
    if not (catalog / "manifest.yaml").is_file():
        raise DevPlaneError(f"catalog manifest not found: {catalog / 'manifest.yaml'}")
    if workflow == "speckit":
        _require_executables("specify")
    project.mkdir(parents=True, exist_ok=True)
    config_dir = project / ".devplane"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "apiVersion": "devplane.dev/v1",
        "kind": "AgentProject",
        "metadata": {"name": project.name},
        "spec": {
            "catalog": {"source": os.path.relpath(catalog, project)},
            "capabilities": [],
        },
    }
    if workflow == "speckit":
        config["spec"]["workflow"] = {
            "engine": "speckit",
            "specifyVersion": f"{SUPPORTED_SPECKIT_PREFIX}x",
        }
    if runtime != "none":
        config["spec"]["runtime"] = {"adapter": runtime}
    (config_dir / "project.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    if workflow == "speckit":
        selected = integration or (DEFAULT_ADAPTER if runtime == "none" else runtime)
        _ensure_project_rules(project, selected)
        _exclude_speckit_run_state(project)
        commands.run_checked(
            ["specify", "init", ".", "--integration", selected, "--force"],
            project,
        )


@app.command()
def init(
    project: Annotated[Path, typer.Argument(help="Project directory")],
    catalog: Annotated[Path, typer.Option("--catalog", help="Local capability catalog")],
    workflow: Annotated[
        str, typer.Option("--workflow", help="Workflow engine: speckit or none")
    ] = "speckit",
    runtime: Annotated[
        str, typer.Option("--runtime", help="Agent adapter or none")
    ] = DEFAULT_ADAPTER,
    integration: Annotated[
        str | None,
        typer.Option("--integration", help="Agent for Spec Kit phases; defaults to the runtime"),
    ] = None,
) -> None:
    """Initialize a catalog project, optionally with workflow and agent runtime."""
    project = _project(project)
    catalog = catalog.expanduser().resolve()
    try:
        _initialize_project(project, catalog, workflow, runtime, integration)
    except (DevPlaneError, OSError) as exc:
        error = exc if isinstance(exc, DevPlaneError) else DevPlaneError(f"cannot initialize project: {exc}")
        _fail(error)
    typer.echo(f"initialized {project}")


@app.command()
def adapters() -> None:
    """List built-in agent runtime adapters."""
    typer.echo(
        yaml.safe_dump(
            {
                "adapters": [
                    {"id": adapter, "executable": adapter}
                    for adapter in list_adapters()
                ]
            },
            sort_keys=False,
        ),
        nl=False,
    )


def _snapshot_generated(project: Path) -> dict[Path, bytes]:
    generated = project / ".devplane" / "generated"
    if generated.is_symlink():
        raise DevPlaneError("generated state must not contain symlinks")
    if not generated.is_dir():
        return {}
    snapshot: dict[Path, bytes] = {}
    for path in generated.rglob("*"):
        if path.is_symlink():
            raise DevPlaneError("generated state must not contain symlinks")
        if path.is_file():
            snapshot[path.relative_to(generated)] = path.read_bytes()
    return snapshot


def _restore_generated(project: Path, snapshot: dict[Path, bytes]) -> None:
    generated = project / ".devplane" / "generated"
    if generated.exists():
        shutil.rmtree(generated)
    for relative, content in snapshot.items():
        output = generated / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)


def _rollback_project_state(
    project: Path,
    config_path: Path,
    original: bytes,
    generated_snapshot: dict[Path, bytes],
) -> list[OSError]:
    errors: list[OSError] = []
    try:
        config_path.write_bytes(original)
    except OSError as exc:
        errors.append(exc)
    try:
        _restore_generated(project, generated_snapshot)
    except OSError as exc:
        errors.append(exc)
    return errors


@app.command("runtime")
def runtime_command(
    adapter: Annotated[str, typer.Argument(help="Agent adapter or none")],
    model: Annotated[
        str | None, typer.Option("--model", help="Adapter model identifier")
    ] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="Adapter provider identifier")
    ] = None,
    executable: Annotated[
        str | None, typer.Option("--executable", help="Executable override")
    ] = None,
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Select an agent adapter without changing the organizational catalog."""
    project = _project(project)
    config_path = project / ".devplane" / "project.yaml"
    original: bytes | None = None
    generated_snapshot: dict[Path, bytes] = {}
    try:
        generated_snapshot = _snapshot_generated(project)
        if adapter != "none" and adapter not in list_adapters():
            raise DevPlaneError(f"unsupported agent adapter: {adapter}")
        original = config_path.read_bytes()
        config = yaml.safe_load(original)
        if not isinstance(config, dict) or not isinstance(config.get("spec"), dict):
            raise DevPlaneError("invalid AgentProject while selecting runtime")
        if adapter == "none":
            config["spec"].pop("runtime", None)
        else:
            selected = {"adapter": adapter}
            if executable is not None:
                selected["executable"] = executable
            if provider is not None:
                selected["provider"] = provider
            if model is not None:
                selected["model"] = model
            config["spec"]["runtime"] = selected
        config_path.write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )
        resolved = sync_project(project, check=False)
        for command in resolved["spec"]["commands"]:
            write_context_bundle(project, command)
    except (DevPlaneError, OSError, UnicodeError, yaml.YAMLError) as exc:
        rollback_errors: list[OSError] = []
        if original is not None:
            rollback_errors = _rollback_project_state(
                project, config_path, original, generated_snapshot
            )
        if rollback_errors:
            details = "; ".join(str(error) for error in rollback_errors)
            _fail(DevPlaneError(f"cannot select runtime: {exc}; rollback failed: {details}"))
        error = (
            exc
            if isinstance(exc, DevPlaneError)
            else DevPlaneError(f"cannot select runtime: {exc}")
        )
        _fail(error)
    typer.echo(f"runtime {adapter}")


@app.command("use-profile")
def use_profile(
    profile_id: Annotated[str, typer.Argument(help="Catalog profile ID")],
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Select a reusable catalog profile and regenerate resolved policy."""
    project = _project(project)
    config_path = project / ".devplane" / "project.yaml"
    original: bytes | None = None
    generated_snapshot: dict[Path, bytes] = {}
    try:
        generated_snapshot = _snapshot_generated(project)
        original = config_path.read_bytes()
        config = yaml.safe_load(original)
        if not isinstance(config, dict) or not isinstance(config.get("spec"), dict):
            raise DevPlaneError("invalid AgentProject while selecting profile")
        config["spec"]["profile"] = profile_id
        config_path.write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )
        resolved = sync_project(project, check=False)
        for command in resolved["spec"]["commands"]:
            write_context_bundle(project, command)
    except (DevPlaneError, OSError, UnicodeError, yaml.YAMLError) as exc:
        rollback_errors: list[OSError] = []
        if original is not None:
            rollback_errors = _rollback_project_state(
                project, config_path, original, generated_snapshot
            )
        if rollback_errors:
            details = "; ".join(str(error) for error in rollback_errors)
            _fail(DevPlaneError(f"cannot select profile: {exc}; rollback failed: {details}"))
        error = (
            exc
            if isinstance(exc, DevPlaneError)
            else DevPlaneError(f"cannot select profile: {exc}")
        )
        _fail(error)
    typer.echo(f"profile {profile_id}")


@app.command("new")
def new_project(
    project: Annotated[Path, typer.Argument(help="New empty project directory")],
    catalog: Annotated[Path, typer.Option("--catalog", help="Local capability catalog")],
    workflow: Annotated[
        str, typer.Option("--workflow", help="Workflow engine: speckit or none")
    ] = "speckit",
    runtime: Annotated[
        str, typer.Option("--runtime", help="Agent adapter or none")
    ] = DEFAULT_ADAPTER,
    integration: Annotated[
        str | None,
        typer.Option("--integration", help="Agent for Spec Kit phases; defaults to the runtime"),
    ] = None,
) -> None:
    """Create a governed greenfield project with a clean Git baseline."""
    project = _project(project)
    catalog = catalog.expanduser().resolve()
    try:
        if project.exists() and any(project.iterdir()):
            raise DevPlaneError(f"new project directory must be empty: {project}")
        _validate_modes(workflow, runtime, integration)
        if workflow == "speckit":
            _require_executables("specify", "git")
        else:
            _require_executables("git")
        project.mkdir(parents=True, exist_ok=True)
        commands.run_checked(["git", "init", "-b", "main"], project)
        _initialize_project(project, catalog, workflow, runtime, integration)
        resolved = sync_project(project, check=False)
        for command in resolved["spec"]["commands"]:
            write_context_bundle(project, command)
        if workflow == "speckit":
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
    """Validate catalog, generated state, and configured workflow integration."""
    project = _project(project)
    try:
        raw = yaml.safe_load(
            (project / ".devplane" / "project.yaml").read_text(encoding="utf-8")
        )
        source_spec = raw.get("spec", {}) if isinstance(raw, dict) else {}
        source_workflow = (
            source_spec.get("workflow") if isinstance(source_spec, dict) else None
        )
        if isinstance(source_workflow, dict) and source_workflow.get("engine") == "speckit":
            if not (project / ".specify").is_dir():
                raise DevPlaneError(
                    f"Spec Kit project directory missing: {project / '.specify'}"
                )
            selected = _read_integration_state(project)["default_integration"]
            commands_dir = SPECKIT_INTEGRATIONS[selected].commands_dir
            if not (project / commands_dir).is_dir():
                raise DevPlaneError(
                    f"Spec Kit {selected} integration marker missing: {commands_dir}"
                )
        resolved = sync_project(project, check=True)
        _verify_contexts(project, resolved)
        workflow = resolved["spec"].get("workflow")
        if isinstance(workflow, dict) and workflow.get("engine") == "speckit":
            _read_integration_state(project)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        _fail(DevPlaneError(f"cannot validate project: {exc}"))
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
            settings = _phase_settings(project, resolved)
            write_feature_request(project, effective_run_id, spec)
            phase_result = execute_phase(
                project,
                effective_run_id,
                "specify",
                spec,
                settings=settings,
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
                    "integration": settings.integration,
                    "run_id": effective_run_id,
                    "external_run_id": phase_result["run_id"],
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "manifest_digest": state.manifest_digest,
                    "spec_digest": state.spec_digest,
                },
            )
            typer.echo(json.dumps(state.to_mapping(), indent=2, sort_keys=True))
            return
        integration = _read_integration_state(project)["default_integration"]
        result = commands.run_checked(
            [
                "specify", "workflow", "run", "speckit",
                "--input", f"spec={spec}",
                "--input", f"integration={integration}",
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
        "integration": integration,
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
                settings=_phase_settings(project, resolved),
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
        # Projects created before this exclusion existed still need it here.
        _exclude_speckit_run_state(project)
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
            raise DevPlaneError(
                f"project must be clean before creating an execution plan{commands.describe_dirty(status)}"
            )
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
            validation_commands=validation or resolved["spec"].get("validations", []),
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
    parallel: Annotated[bool, typer.Option("--parallel", help="Use isolated agent sessions")] = False,
    max_agents: Annotated[int, typer.Option("--max-agents", min=1, max=8)] = 3,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the execution plan without launching agents")] = False,
    project: Annotated[Path, typer.Option("--project", help="Project root")] = Path("."),
) -> None:
    """Implement governed tasks with bounded parallel agent sessions."""
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
            if plan.source_tasks == BACKLOG_SOURCE:
                verify_backlog_source(project, plan)
            else:
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
            runtime_config = runtime_from_manifest(resolved)
            pipeline_result = run_execution_pipeline(
                project,
                plan,
                max_agents=max_agents,
                runtime_config=runtime_config,
            )
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
                        "adapter": runtime_config.adapter,
                        "provider": runtime_config.provider,
                        "model": runtime_config.model,
                        **asdict(assignment),
                    },
                )
            typer.echo(json.dumps(asdict(pipeline_result), indent=2, sort_keys=True))
            if pipeline_result.status != "completed":
                raise typer.Exit(1)
            return
        assert tasks_file is not None
        runtime_config = None if dry_run else runtime_from_manifest(resolved)
        result = run_parallel_implementation(
            project,
            tasks_file,
            manifest_digest=resolved["metadata"]["sourceHash"],
            max_agents=max_agents,
            dry_run=dry_run,
            runtime_config=runtime_config,
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
                    "adapter": runtime_config.adapter if runtime_config else None,
                    "provider": runtime_config.provider if runtime_config else None,
                    "model": runtime_config.model if runtime_config else None,
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
    backlog_review: list[str] | None = None
    backlog_error: str | None = None
    if plan.source_tasks == BACKLOG_SOURCE:
        try:
            backlog_review = mark_ready_for_review(project, plan)
        except DevPlaneError as exc:
            backlog_error = str(exc)
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
    if backlog_review:
        _append_audit(
            project,
            {
                "event": "backlog.review.recorded",
                "run_id": run_id,
                "recorded_at": datetime.now(UTC).isoformat(),
                "final_commit": final_commit,
                "stories": backlog_review,
            },
        )
    payload: dict[str, object] = {"run_id": run_id, "status": "integrated", "final_commit": final_commit}
    if backlog_review is not None:
        payload["backlog_review"] = backlog_review
    if backlog_error is not None:
        payload["backlog_error"] = (
            f"integrated, but the backlog was not updated: {backlog_error}. "
            "Move the stories with `devplane backlog status`."
        )
    typer.echo(json.dumps(payload, indent=2))
    if backlog_error is not None:
        raise typer.Exit(1)


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
        if plan.source_tasks == BACKLOG_SOURCE:
            verify_backlog_source(project, plan)
        else:
            source = (project / plan.source_tasks).resolve()
            if not source.is_relative_to(project) or not source.is_file():
                raise DevPlaneError("execution plan tasks source is unavailable")
            phases = parse_tasks_markdown(source.read_text(encoding="utf-8"))
            if compute_tasks_digest(phases) != plan.tasks_digest:
                raise DevPlaneError("cannot retry after tasks drift")
        runtime_config = runtime_from_manifest(resolved)
        retried = run_execution_pipeline(
            project,
            plan,
            max_agents=max_agents,
            runtime_config=runtime_config,
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
    if retried.status != "completed":
        raise typer.Exit(1)


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
        integration = _read_integration_state(project)["default_integration"]
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
            "integration": integration,
        },
    )
    typer.echo(json.dumps(workflow_result, indent=2, sort_keys=True))


_GUARD_INPUT_LIMIT = 1_048_576


def _guard_verdict(policy_path: Path) -> str | None:
    """Return a denial reason, or None when the hook event is allowed."""
    try:
        policy = load_policy(policy_path)
        raw = sys.stdin.read(_GUARD_INPUT_LIMIT + 1)
        if len(raw) > _GUARD_INPUT_LIMIT:
            return "hook event exceeds the size limit"
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return "hook event is not valid JSON"
        decision = decide(policy, event)
    except Exception as exc:  # noqa: BLE001 - a guard must fail closed on anything
        return f"{type(exc).__name__}: {exc}"
    return None if decision.allowed else decision.reason


@app.command("guard")
def guard_command(
    policy: Annotated[
        Path, typer.Option("--policy", help="Guard policy JSON written by DevPlane")
    ],
) -> None:
    """Claude Code PreToolUse hook: read one event on stdin; exit 2 blocks the tool call.

    Claude Code treats only exit code 2 as a block, so every failure path,
    including unexpected errors, exits with 2.
    """
    reason = _guard_verdict(policy)
    if reason is not None:
        typer.echo(f"DevPlane guard: {reason}", err=True)
        raise typer.Exit(2)


backlog_app = typer.Typer(
    help="Repository-scoped story backlog stored in backlog/*.md", no_args_is_help=True
)
app.add_typer(backlog_app, name="backlog")

_ProjectOption = Annotated[Path, typer.Option("--project", help="Project root")]


def _story_summary(story: Story) -> dict[str, object]:
    return {
        "id": story.id,
        "title": story.title,
        "status": story.status,
        "priority": story.priority,
        "depends_on": list(story.depends_on),
        "write_scope": list(story.write_scope),
        "validation": list(story.validation),
        "feature": story.feature,
    }


def _emit(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@backlog_app.command("check")
def backlog_check(project: _ProjectOption = Path(".")) -> None:
    """Validate every story and the dependency graph."""
    try:
        backlog = load_backlog(_project(project))
    except DevPlaneError as exc:
        _fail(exc)
    _emit({"stories": len(backlog.stories)})


@backlog_app.command("list")
def backlog_list(
    status: Annotated[str | None, typer.Option("--status", help="Only stories in this status")] = None,
    project: _ProjectOption = Path("."),
) -> None:
    """List stories as JSON, optionally filtered by status."""
    try:
        if status is not None and status not in STATUSES:
            raise DevPlaneError(f"unknown status: {status}")
        backlog = load_backlog(_project(project))
    except DevPlaneError as exc:
        _fail(exc)
    _emit([_story_summary(item) for item in backlog.stories if status in (None, item.status)])


@backlog_app.command("show")
def backlog_show(story_id: str, project: _ProjectOption = Path(".")) -> None:
    """Show one story, including its Markdown body."""
    try:
        story = load_backlog(_project(project)).get(story_id)
    except DevPlaneError as exc:
        _fail(exc)
    _emit({**_story_summary(story), "body": story.body})


@backlog_app.command("next")
def backlog_next(project: _ProjectOption = Path(".")) -> None:
    """List ready stories whose dependencies are done, highest priority first."""
    try:
        stories = ready_stories(load_backlog(_project(project)))
    except DevPlaneError as exc:
        _fail(exc)
    _emit([_story_summary(item) for item in stories])


@backlog_app.command("plan")
def backlog_plan(
    max_parallel: Annotated[int, typer.Option("--max-parallel", help="Stories per batch")] = 2,
    project: _ProjectOption = Path("."),
) -> None:
    """Group ready stories into batches with pairwise disjoint write scopes."""
    try:
        plan = plan_batches(load_backlog(_project(project)), max_parallel=max_parallel)
    except DevPlaneError as exc:
        _fail(exc)
    _emit(
        {
            "batches": [[story.id for story in batch] for batch in plan.batches],
            "deferred": plan.deferred,
            "waiting": plan.waiting,
        }
    )


@backlog_app.command("status")
def backlog_status(
    story_id: str, new_status: str, project: _ProjectOption = Path(".")
) -> None:
    """Move a story to a new status if the transition is allowed."""
    root = _project(project)
    try:
        previous = load_backlog(root).get(story_id).status
        story = set_status(root, story_id, new_status)
    except DevPlaneError as exc:
        _fail(exc)
    _append_audit(
        root,
        {
            "event": "backlog.status.changed",
            "recorded_at": datetime.now(UTC).isoformat(),
            "story": story.id,
            "from": previous,
            "to": story.status,
        },
    )
    _emit({"id": story.id, "status": story.status})


@backlog_app.command("run")
def backlog_run_command(
    validation: Annotated[
        list[str] | None,
        typer.Option("--validation", help="Exact validation command; repeat as needed"),
    ] = None,
    max_parallel: Annotated[
        int, typer.Option("--max-parallel", min=1, max=8, help="Stories claimed together")
    ] = 2,
    max_agents: Annotated[int, typer.Option("--max-agents", min=1, max=8)] = 3,
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Stable safe identifier; generated when omitted")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the batch and plan without claiming anything")
    ] = False,
    project: _ProjectOption = Path("."),
) -> None:
    """Claim the next batch of ready stories and implement them with parallel agents.

    Claiming commits ``ready -> in_progress`` for the batch; that commit is the
    plan's approved base. Review the result and run ``devplane integrate <run>``.
    """
    project = _project(project)
    try:
        resolved = sync_project(project, check=True)
        _verify_contexts(project, resolved)
        effective_run_id = run_id or f"backlog-{uuid.uuid4().hex[:12]}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", effective_run_id):
            raise DevPlaneError("run_id must be a safe identifier")
        state_dir = project / ".git" / "devplane" / "runs" / effective_run_id
        if state_dir.exists():
            raise DevPlaneError(f"run already exists: {effective_run_id}")
        runtime_config = None if dry_run else runtime_from_manifest(resolved)
        backlog = load_backlog(project)
        planned = plan_batches(backlog, max_parallel=max_parallel)
        if not planned.batches:
            notes = [f"{key}: {value}" for key, value in sorted(planned.deferred.items())]
            notes += [f"{key} waits for {', '.join(value)}" for key, value in sorted(planned.waiting.items())]
            raise DevPlaneError(
                "no ready stories to run" + (f" ({'; '.join(notes)})" if notes else "")
            )
        batch = list(planned.batches[0])
        validations = validation or resolved["spec"].get("validations", [])
        head = commands.run_checked(["git", "rev-parse", "HEAD"], project).stdout.strip()
        # Validate the whole plan before anything is claimed or committed.
        preview = build_backlog_plan(
            batch,
            run_id=effective_run_id,
            manifest_digest=resolved["metadata"]["sourceHash"],
            base_commit=head,
            validation_commands=validations,
        )
        if dry_run:
            _emit(
                {
                    "run_id": effective_run_id,
                    "dry_run": True,
                    "stories": [story.id for story in batch],
                    "deferred": planned.deferred,
                    "assignments": [asdict(item) for item in preview.assignments],
                }
            )
            return
        assert runtime_config is not None
        base_commit = claim_stories(project, batch)
        plan = build_backlog_plan(
            batch,
            run_id=effective_run_id,
            manifest_digest=resolved["metadata"]["sourceHash"],
            base_commit=base_commit,
            validation_commands=validations,
        )
        plan_path = state_dir / "execution-plan.yaml"
        write_execution_plan(plan, plan_path)
        _append_audit(
            project,
            {
                "event": "backlog.run.started",
                "run_id": effective_run_id,
                "recorded_at": datetime.now(UTC).isoformat(),
                "manifest_digest": resolved["metadata"]["sourceHash"],
                "base_commit": base_commit,
                "stories": [story.id for story in batch],
            },
        )
        result = run_execution_pipeline(
            project, plan, max_agents=max_agents, runtime_config=runtime_config
        )
        write_pipeline_run(result, state_dir / "result.json")
        for assignment in result.assignments:
            _append_audit(
                project,
                {
                    "event": "pipeline.assignment",
                    "run_id": result.run_id,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "manifest_digest": resolved["metadata"]["sourceHash"],
                    "adapter": runtime_config.adapter,
                    "provider": runtime_config.provider,
                    "model": runtime_config.model,
                    **asdict(assignment),
                },
            )
    except (DevPlaneError, OSError) as exc:
        _fail(exc if isinstance(exc, DevPlaneError) else DevPlaneError(f"cannot run backlog: {exc}"))
    _emit({**asdict(result), "stories": [story.id for story in batch]})
    if result.status != "completed":
        raise typer.Exit(1)
