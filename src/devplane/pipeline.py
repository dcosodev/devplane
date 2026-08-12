"""Dependency-aware execution of governed plans in isolated Git worktrees."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shlex
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from .agent_runtime import AgentRuntimeConfig, SessionRequest, SessionResult
from .core import DevPlaneError
from .execution_plan import ExecutionAssignment, ExecutionPlan
from .worktrees import WorktreeHandle

_SAFE_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SHELL_OPERATORS = {"&&", "||", "|", ";", ">", ">>", "<", "2>", "&"}


@dataclass(frozen=True)
class AssignmentRun:
    assignment_id: str
    phase: str
    status: str
    base_commit: str | None = None
    branch: str | None = None
    worktree: str | None = None
    commit: str | None = None
    session_id: str | None = None
    changed_paths: tuple[str, ...] = ()
    output_digest: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PipelineRun:
    run_id: str
    status: str
    base_commit: str
    final_commit: str | None
    final_branch: str | None
    final_worktree: str | None
    assignments: tuple[AssignmentRun, ...]


def write_pipeline_run(result: PipelineRun, path: Path) -> None:
    """Persist a sanitized pipeline result atomically in local Git state."""
    payload = asdict(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_pipeline_run(path: Path) -> PipelineRun:
    """Load persisted pipeline state without trusting its executable fields."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DevPlaneError(f"invalid pipeline result: {path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("assignments"), list):
        raise DevPlaneError(f"invalid pipeline result structure: {path}")
    assignments: list[AssignmentRun] = []
    for row in data["assignments"]:
        if not isinstance(row, dict) or not isinstance(row.get("assignment_id"), str):
            raise DevPlaneError("invalid pipeline assignment result")
        changed = row.get("changed_paths", [])
        if not isinstance(changed, list) or not all(isinstance(item, str) for item in changed):
            raise DevPlaneError("invalid changed paths in pipeline result")
        commit = row.get("commit")
        if commit is not None and (not isinstance(commit, str) or not _SAFE_COMMIT.fullmatch(commit)):
            raise DevPlaneError("invalid assignment commit in pipeline result")
        assignments.append(
            AssignmentRun(
                assignment_id=row["assignment_id"],
                phase=str(row.get("phase", "")),
                status=str(row.get("status", "")),
                base_commit=row.get("base_commit"),
                branch=row.get("branch"),
                worktree=row.get("worktree"),
                commit=commit,
                session_id=row.get("session_id"),
                changed_paths=tuple(changed),
                output_digest=row.get("output_digest"),
                error=row.get("error"),
            )
        )
    base_commit = data.get("base_commit")
    final_commit = data.get("final_commit")
    if not isinstance(base_commit, str) or not _SAFE_COMMIT.fullmatch(base_commit):
        raise DevPlaneError("invalid base commit in pipeline result")
    if final_commit is not None and (
        not isinstance(final_commit, str) or not _SAFE_COMMIT.fullmatch(final_commit)
    ):
        raise DevPlaneError("invalid final commit in pipeline result")
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_id):
        raise DevPlaneError("invalid run id in pipeline result")
    return PipelineRun(
        run_id=run_id,
        status=str(data.get("status", "")),
        base_commit=base_commit,
        final_commit=final_commit,
        final_branch=data.get("final_branch"),
        final_worktree=data.get("final_worktree"),
        assignments=tuple(assignments),
    )


CreateWorktree = Callable[..., WorktreeHandle]
RunSessions = Callable[..., list[SessionResult]]
CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


def _default_create() -> CreateWorktree:
    from .worktrees import create_worktree

    return create_worktree


def _default_sessions(runtime: AgentRuntimeConfig | None = None) -> RunSessions:
    from .agent_runtime import run_sessions

    if runtime is None:
        raise DevPlaneError("agent runtime configuration is required for execution")

    def dispatch(requests, max_workers, runner=None):
        return run_sessions(
            requests,
            max_workers=max_workers,
            runtime=runtime,
            runner=runner,
        )

    return dispatch


def _default_runner() -> CommandRunner:
    from .commands import run_checked

    return run_checked


def _session_id(output: str) -> str | None:
    match = re.search(r"(?m)^session_id:\s*(\S+)\s*$", output)
    return match.group(1) if match else None


def _assignment_prompt(assignment: ExecutionAssignment, plan: ExecutionPlan) -> str:
    tasks = "\n".join(f"- {task}" for task in assignment.tasks)
    scopes = "\n".join(f"- {path}" for path in assignment.allowed_paths)
    validation = "\n".join(f"- {command}" for command in assignment.validation)
    final_contract_rules = (
        "7. Si no hace falta editar, usa `git commit --allow-empty` solo si la tarea es "
        "exclusivamente de validación y la validación pasó.\n"
        "8. No combines comandos con operadores de shell (`&&`, `||`, `;`, pipes); "
        "ejecuta cada comando por separado.\n"
        "9. Informa SHA, archivos, comandos, resultados y riesgos."
        if assignment.allow_empty_commit
        else "7. No combines comandos con operadores de shell (`&&`, `||`, `;`, pipes); "
        "ejecuta cada comando por separado.\n"
        "8. Informa SHA, archivos, comandos, resultados y riesgos."
    )
    return f"""Trabaja como agente escritor dentro de un git worktree aislado.

Asignación: {assignment.assignment_id}
Fase: {assignment.phase}
Tareas:
{tasks}

Paths de escritura permitidos:
{scopes}

Validación obligatoria:
{validation}

Contrato:
1. Lee AGENTS.md, `.devplane/generated/context-implement.md`, spec.md, plan.md y tasks.md aplicables antes de editar.
2. No escribas fuera de los paths permitidos; DevPlane rechazará el commit.
3. Sigue TDD y ejecuta la validación indicada.
4. No modifiques .devplane/, .git/, .hermes/, .specify/ ni specs/.
5. No hagas push, deploy, cambios de cuenta ni operaciones Git destructivas.
6. Crea un único commit local focalizado y deja el worktree limpio.
{final_contract_rules}

Manifiesto: {plan.manifest_digest}
Tasks: {plan.tasks_digest}
"""


def _command_argv(command: str) -> list[str]:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise DevPlaneError("invalid validation command") from exc
    if not argv or any(token in _SHELL_OPERATORS for token in argv):
        raise DevPlaneError("validation commands must not contain shell operators")
    return argv


def _safe_changed_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DevPlaneError("commit contains an unsafe changed path")
    if path.parts[0] in {".devplane", ".git", ".hermes", ".specify", "specs"}:
        raise DevPlaneError(f"changed path is governed and read-only: {value}")
    return path.as_posix()


def _is_allowed(path: str, scopes: tuple[str, ...]) -> bool:
    return any(path == scope or fnmatch.fnmatchcase(path, scope) for scope in scopes)


def _assignment_batches(assignments: tuple[ExecutionAssignment, ...]) -> list[list[ExecutionAssignment]]:
    batches: list[list[ExecutionAssignment]] = []
    index = 0
    while index < len(assignments):
        item = assignments[index]
        if item.mode != "parallel":
            batches.append([item])
            index += 1
            continue
        batch = [item]
        index += 1
        while index < len(assignments):
            candidate = assignments[index]
            if candidate.mode != "parallel" or candidate.depends_on != item.depends_on:
                break
            batch.append(candidate)
            index += 1
        batches.append(batch)
    return batches


def _blocked(assignment: ExecutionAssignment) -> AssignmentRun:
    return AssignmentRun(
        assignment_id=assignment.assignment_id,
        phase=assignment.phase,
        status="blocked",
        error="blocked by a failed earlier batch",
    )


def run_execution_pipeline(
    project: Path,
    plan: ExecutionPlan,
    *,
    max_agents: int,
    create_fn: CreateWorktree | None = None,
    sessions_fn: RunSessions | None = None,
    runtime_config: AgentRuntimeConfig | None = None,
    command_runner: CommandRunner | None = None,
    prior_result: PipelineRun | None = None,
) -> PipelineRun:
    """Execute a plan cumulatively while leaving the project's branch untouched."""
    if not isinstance(max_agents, int) or isinstance(max_agents, bool) or not 1 <= max_agents <= 8:
        raise DevPlaneError("max_agents must be an integer between 1 and 8")
    project = project.expanduser().resolve()
    create = create_fn or _default_create()
    run_many = sessions_fn or _default_sessions(runtime_config)
    run_command = command_runner or _default_runner()

    status = run_command(["git", "status", "--porcelain"], project).stdout
    if status:
        raise DevPlaneError("project must be clean before executing a plan")
    current_head = run_command(["git", "rev-parse", "HEAD"], project).stdout.strip()
    if current_head != plan.base_commit:
        raise DevPlaneError(
            f"execution plan base commit drift: expected {plan.base_commit}, found {current_head}"
        )

    prior_by_id: dict[str, AssignmentRun] = {}
    if prior_result is not None:
        if prior_result.run_id != plan.run_id or prior_result.base_commit != plan.base_commit:
            raise DevPlaneError("retry result does not match the execution plan")
        prior_by_id = {item.assignment_id: item for item in prior_result.assignments}

    all_results: list[AssignmentRun] = []
    current_base = plan.base_commit
    final_handle: WorktreeHandle | None = None
    failed = False

    batches = _assignment_batches(plan.assignments)
    for batch_number, batch in enumerate(batches, start=1):
        if failed:
            all_results.extend(_blocked(assignment) for assignment in batch)
            continue
        completed_ids = {item.assignment_id for item in all_results if item.status == "completed"}
        if any(set(assignment.depends_on) - completed_ids for assignment in batch):
            all_results.extend(_blocked(assignment) for assignment in batch)
            failed = True
            continue

        handles: dict[str, WorktreeHandle] = {}
        batch_runs: dict[str, AssignmentRun] = {}
        for assignment in batch:
            previous = prior_by_id.get(assignment.assignment_id)
            if (
                previous is not None
                and previous.status == "completed"
                and previous.base_commit == current_base
                and previous.commit is not None
            ):
                batch_runs[assignment.assignment_id] = previous
                continue
            try:
                worktree_id = assignment.assignment_id
                if previous is not None:
                    worktree_id = f"{assignment.assignment_id}-retry-{uuid.uuid4().hex[:8]}"
                handle = create(
                    project,
                    plan.run_id,
                    worktree_id,
                    current_base,
                    runner=run_command,
                )
            except DevPlaneError as exc:
                batch_runs[assignment.assignment_id] = AssignmentRun(
                    assignment.assignment_id,
                    assignment.phase,
                    "failed",
                    base_commit=current_base,
                    error=str(exc),
                )
                failed = True
            else:
                handles[assignment.assignment_id] = handle

        requests = [
            SessionRequest(
                task_id=assignment.assignment_id,
                prompt=_assignment_prompt(assignment, plan),
                cwd=handles[assignment.assignment_id].path,
            )
            for assignment in batch
            if assignment.assignment_id in handles
        ]
        try:
            sessions = (
                run_many(
                    requests,
                    max_workers=min(max_agents, max(1, len(requests))),
                    runner=run_command,
                )
                if requests
                else []
            )
        except Exception as exc:  # noqa: BLE001 - preserve evidence when a launcher aborts
            sessions = []
            for assignment in batch:
                handle = handles.get(assignment.assignment_id)
                if handle is not None:
                    batch_runs[assignment.assignment_id] = AssignmentRun(
                        assignment.assignment_id,
                        assignment.phase,
                        "failed",
                        base_commit=current_base,
                        branch=handle.branch,
                        worktree=str(handle.path),
                        error=f"session launcher aborted: {type(exc).__name__}",
                    )
            failed = True
        session_map = {session.task_id: session for session in sessions}

        for assignment in batch:
            if assignment.assignment_id in batch_runs:
                continue
            handle = handles[assignment.assignment_id]
            session = session_map.get(assignment.assignment_id)
            if session is None or session.returncode != 0:
                batch_runs[assignment.assignment_id] = AssignmentRun(
                    assignment.assignment_id,
                    assignment.phase,
                    "failed",
                    base_commit=current_base,
                    branch=handle.branch,
                    worktree=str(handle.path),
                    session_id=_session_id(f"{session.stdout}\n{session.stderr}") if session else None,
                    error=(session.error if session else None) or "agent session failed",
                )
                failed = True
                continue
            sid = _session_id(f"{session.stdout}\n{session.stderr}")
            digest = f"sha256:{hashlib.sha256(session.stdout.encode('utf-8')).hexdigest()}"
            head = run_command(["git", "rev-parse", "HEAD"], handle.path).stdout.strip()
            if not _SAFE_COMMIT.fullmatch(head) or head == current_base:
                batch_runs[assignment.assignment_id] = AssignmentRun(
                    assignment.assignment_id,
                    assignment.phase,
                    "failed",
                    base_commit=current_base,
                    branch=handle.branch,
                    worktree=str(handle.path),
                    session_id=sid,
                    output_digest=digest,
                    error="agent session completed without a valid commit",
                )
                failed = True
                continue
            dirty = run_command(["git", "status", "--porcelain"], handle.path).stdout
            changed_output = run_command(
                ["git", "diff", "--name-only", f"{current_base}..{head}"], handle.path
            ).stdout
            try:
                changed = tuple(
                    _safe_changed_path(path)
                    for path in changed_output.splitlines()
                    if path.strip()
                )
            except DevPlaneError as exc:
                changed = ()
                error = str(exc)
            else:
                outside = [path for path in changed if not _is_allowed(path, assignment.allowed_paths)]
                error = (
                    f"commit changed files outside allowed paths: {outside[0]}"
                    if outside
                    else None
                )
            if dirty:
                error = error or "agent left uncommitted worktree changes"
            if not changed and not assignment.allow_empty_commit:
                error = error or "agent commit has no changed files"
            if error is None:
                try:
                    run_command(["git", "diff", "--check", f"{current_base}..{head}"], handle.path)
                    for command in assignment.validation:
                        run_command(_command_argv(command), handle.path)
                except DevPlaneError as exc:
                    error = str(exc)
            if error is not None:
                batch_runs[assignment.assignment_id] = AssignmentRun(
                    assignment.assignment_id,
                    assignment.phase,
                    "failed",
                    base_commit=current_base,
                    branch=handle.branch,
                    worktree=str(handle.path),
                    commit=head,
                    session_id=sid,
                    changed_paths=changed,
                    output_digest=digest,
                    error=error,
                )
                failed = True
                continue
            batch_runs[assignment.assignment_id] = AssignmentRun(
                assignment.assignment_id,
                assignment.phase,
                "completed",
                base_commit=current_base,
                branch=handle.branch,
                worktree=str(handle.path),
                commit=head,
                session_id=sid,
                changed_paths=changed,
                output_digest=digest,
            )

        ordered_batch_runs = [batch_runs[item.assignment_id] for item in batch]
        all_results.extend(ordered_batch_runs)
        if failed:
            continue

        if len(batch) == 1:
            current_base = ordered_batch_runs[0].commit or current_base
            final_handle = handles.get(batch[0].assignment_id)
            continue

        integration_id = f"batch-{batch_number}-integration"
        if prior_result is not None:
            integration_id = f"{integration_id}-retry-{uuid.uuid4().hex[:8]}"
        try:
            integration = create(
                project,
                plan.run_id,
                integration_id,
                current_base,
                runner=run_command,
            )
            for assignment, item in zip(batch, ordered_batch_runs):
                if item.commit is None:
                    raise DevPlaneError("parallel assignment has no commit")
                run_command(["git", "cherry-pick", item.commit], integration.path)
                for command in assignment.validation:
                    run_command(_command_argv(command), integration.path)
            merged = run_command(["git", "rev-parse", "HEAD"], integration.path).stdout.strip()
            if not _SAFE_COMMIT.fullmatch(merged):
                raise DevPlaneError("integration branch has an invalid commit")
        except DevPlaneError as exc:
            failed = True
            # Preserve all writer commits and the integration worktree for inspection.
            last_index = len(all_results) - 1
            all_results[last_index] = replace(
                all_results[last_index],
                status="failed",
                error=f"batch integration failed: {exc}",
            )
        else:
            current_base = merged
            final_handle = integration

    final_status = "failed" if failed else "completed"
    return PipelineRun(
        run_id=plan.run_id,
        status=final_status,
        base_commit=plan.base_commit,
        final_commit=current_base if not failed else None,
        final_branch=final_handle.branch if not failed and final_handle else None,
        final_worktree=str(final_handle.path) if not failed and final_handle else None,
        assignments=tuple(all_results),
    )


def integrate_pipeline(
    project: Path,
    plan: ExecutionPlan,
    result: PipelineRun,
    *,
    command_runner: CommandRunner | None = None,
) -> str:
    """Fast-forward the project only after an explicit integration action."""
    project = project.expanduser().resolve()
    run_command = command_runner or _default_runner()
    if result.run_id != plan.run_id or result.status != "completed" or result.final_commit is None:
        raise DevPlaneError("only a completed matching pipeline run can be integrated")
    if not _SAFE_COMMIT.fullmatch(result.final_commit):
        raise DevPlaneError("pipeline final commit is invalid")
    if run_command(["git", "status", "--porcelain"], project).stdout:
        raise DevPlaneError("project must be clean before integration")
    head = run_command(["git", "rev-parse", "HEAD"], project).stdout.strip()
    if head != plan.base_commit:
        raise DevPlaneError(
            f"cannot integrate after project HEAD drift: expected {plan.base_commit}, found {head}"
        )
    try:
        run_command(
            ["git", "merge-base", "--is-ancestor", plan.base_commit, result.final_commit],
            project,
        )
    except DevPlaneError as exc:
        raise DevPlaneError("pipeline final commit is not descended from the approved base") from exc
    if result.final_worktree is None:
        raise DevPlaneError("pipeline final worktree is unavailable for the integration gate")
    validation_worktree = Path(result.final_worktree).expanduser().resolve()
    worktree_root = (
        project.parent / ".devplane-worktrees" / project.name / plan.run_id
    ).resolve()
    if (
        not validation_worktree.is_relative_to(worktree_root)
        or not validation_worktree.is_dir()
    ):
        raise DevPlaneError("pipeline final worktree escapes the run root")
    validation_head = run_command(
        ["git", "rev-parse", "HEAD"], validation_worktree
    ).stdout.strip()
    if validation_head != result.final_commit:
        raise DevPlaneError("pipeline final worktree commit drift detected")
    if run_command(["git", "status", "--porcelain"], validation_worktree).stdout:
        raise DevPlaneError("pipeline final worktree must be clean before integration")
    seen_commands: set[str] = set()
    for assignment in plan.assignments:
        for command in assignment.validation:
            if command in seen_commands:
                continue
            run_command(_command_argv(command), validation_worktree)
            seen_commands.add(command)
    run_command(["git", "merge", "--ff-only", result.final_commit], project)
    return result.final_commit
