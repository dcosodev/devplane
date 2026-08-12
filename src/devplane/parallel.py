"""Parallel implementation orchestration for Spec Kit phases."""

from __future__ import annotations

import hashlib
import re
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .agent_runtime import AgentRuntimeConfig, SessionRequest, SessionResult
from .core import DevPlaneError
from .tasks import TaskPhase, build_execution_batches, parse_tasks_markdown
from .worktrees import WorktreeHandle


@dataclass(frozen=True)
class PhaseRun:
    phase: str
    task_id: str
    status: str
    branch: str | None = None
    worktree: str | None = None
    commit: str | None = None
    session_id: str | None = None
    output_digest: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ParallelRun:
    run_id: str
    dry_run: bool
    phases: list[PhaseRun]


CreateWorktree = Callable[..., WorktreeHandle]
RunSessions = Callable[..., list[SessionResult]]
CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


def resolve_tasks_file(project: Path, tasks_file: Path) -> Path:
    """Resolve a tasks file without allowing traversal or symlink escape."""
    root = project.expanduser().resolve()
    candidate = tasks_file.expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise DevPlaneError(f"tasks file escapes project root: {tasks_file}")
    if not resolved.is_file():
        raise DevPlaneError(f"tasks file not found: {resolved}")
    return resolved


def _phase_identifier(index: int, phase: TaskPhase) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", phase.name.lower()).strip("-")
    if not slug:
        slug = "phase"
    return f"p{index + 1}-{slug}"[:63].rstrip("-")


def build_phase_prompt(phase: TaskPhase, manifest_digest: str) -> str:
    """Build a self-contained, auditable prompt for one agent writer."""
    task_lines = "\n".join(
        f"- {task.task_id}: {task.description}"
        for task in phase.tasks
    )
    return f"""Trabaja como agente escritor dentro de un git worktree aislado.

Fase asignada: {phase.name}
Manifiesto DevPlane: {manifest_digest}
Tareas Spec Kit:
{task_lines}

Contrato obligatorio:
1. Lee AGENTS.md, `.devplane/generated/context-implement.md` y las instrucciones locales aplicables antes de editar.
2. Limítate estrictamente a esta fase y a los paths mencionados por sus tareas.
3. Sigue TDD: crea o ajusta una prueba, comprueba el fallo esperado, implementa el mínimo cambio y repite la validación.
4. No modifiques .devplane/, .specify/ ni specs/; son artefactos de gobierno.
5. No hagas push, despliegues, cambios de cuenta ni operaciones Git destructivas.
6. Ejecuta la validación nativa del repositorio.
7. Crea un commit local focalizado antes de terminar.
8. Devuelve el SHA, archivos modificados, comandos ejecutados, resultados exactos y riesgos.
"""


def _session_id(stdout: str) -> str | None:
    match = re.search(r"(?m)^session_id:\s*(\S+)\s*$", stdout)
    return match.group(1) if match else None


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


def run_parallel_implementation(
    project: Path,
    tasks_file: Path,
    *,
    manifest_digest: str,
    max_agents: int,
    dry_run: bool = False,
    create_fn: CreateWorktree | None = None,
    sessions_fn: RunSessions | None = None,
    runtime_config: AgentRuntimeConfig | None = None,
    command_runner: CommandRunner | None = None,
) -> ParallelRun:
    """Dispatch task phases into isolated agent sessions.

    Setup/foundational/unknown phases are dispatched serially. Contiguous user
    story phases share a bounded parallel batch. Successful sessions must leave
    a commit in their worktree; integration is deliberately left to the Hermes
    coordinator after diff review.
    """
    if not isinstance(max_agents, int) or isinstance(max_agents, bool) or not 1 <= max_agents <= 8:
        raise DevPlaneError("max_agents must be an integer between 1 and 8")

    project = project.expanduser().resolve()
    source = resolve_tasks_file(project, tasks_file)
    try:
        phases = parse_tasks_markdown(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DevPlaneError(f"invalid Spec Kit tasks file: {exc}") from exc
    batches = build_execution_batches(phases)
    run_id = uuid.uuid4().hex[:12]

    planned: list[tuple[str, TaskPhase]] = []
    phase_index = {id(phase): index for index, phase in enumerate(phases)}
    for phase in phases:
        planned.append((_phase_identifier(phase_index[id(phase)], phase), phase))

    if dry_run:
        return ParallelRun(
            run_id=run_id,
            dry_run=True,
            phases=[
                PhaseRun(phase=phase.name, task_id=task_id, status="planned")
                for task_id, phase in planned
            ],
        )

    create = create_fn or _default_create()
    run_many = sessions_fn or _default_sessions(runtime_config)
    run_command = command_runner or _default_runner()
    base = run_command(["git", "rev-parse", "HEAD"], project).stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", base):
        raise DevPlaneError("git rev-parse HEAD did not return a valid object id")

    task_ids = {id(phase): task_id for task_id, phase in planned}
    results_by_phase: dict[int, PhaseRun] = {}
    blocked = False

    for batch in batches:
        if blocked:
            for phase in batch:
                task_id = task_ids[id(phase)]
                results_by_phase[id(phase)] = PhaseRun(
                    phase=phase.name,
                    task_id=task_id,
                    status="blocked",
                    error="blocked by a failed earlier batch",
                )
            continue

        handles: list[tuple[TaskPhase, WorktreeHandle]] = []
        create_failed = False
        for phase in batch:
            task_id = task_ids[id(phase)]
            try:
                handle = create(project, run_id, task_id, base, runner=run_command)
            except DevPlaneError as exc:
                create_failed = True
                results_by_phase[id(phase)] = PhaseRun(
                    phase=phase.name,
                    task_id=task_id,
                    status="failed",
                    error=str(exc),
                )
            else:
                handles.append((phase, handle))

        requests = [
            SessionRequest(
                task_id=handle.task_id,
                prompt=build_phase_prompt(phase, manifest_digest),
                cwd=handle.path,
            )
            for phase, handle in handles
        ]
        try:
            session_results = run_many(
                requests,
                max_workers=min(max_agents, max(1, len(requests))),
                runner=run_command,
            ) if requests else []
        except Exception as exc:  # noqa: BLE001 - fail the batch without crashing the coordinator
            for phase, handle in handles:
                results_by_phase[id(phase)] = PhaseRun(
                    phase=phase.name,
                    task_id=handle.task_id,
                    status="failed",
                    branch=handle.branch,
                    worktree=str(handle.path),
                    error=f"session launcher aborted: {type(exc).__name__}",
                )
            blocked = True
            continue
        session_by_id = {result.task_id: result for result in session_results}

        batch_failed = create_failed
        for phase, handle in handles:
            session = session_by_id.get(handle.task_id)
            if session is None:
                batch_failed = True
                results_by_phase[id(phase)] = PhaseRun(
                    phase=phase.name,
                    task_id=handle.task_id,
                    status="failed",
                    branch=handle.branch,
                    worktree=str(handle.path),
                    error="agent session returned no result",
                )
                continue
            digest = f"sha256:{hashlib.sha256(session.stdout.encode('utf-8')).hexdigest()}"
            sid = _session_id(f"{session.stdout}\n{session.stderr}")
            if session.returncode != 0:
                batch_failed = True
                results_by_phase[id(phase)] = PhaseRun(
                    phase=phase.name,
                    task_id=handle.task_id,
                    status="failed",
                    branch=handle.branch,
                    worktree=str(handle.path),
                    session_id=sid,
                    output_digest=digest,
                    error=session.error or "agent session failed",
                )
                continue

            head = run_command(["git", "rev-parse", "HEAD"], handle.path).stdout.strip()
            if head == base or not re.fullmatch(r"[0-9a-fA-F]{7,64}", head):
                batch_failed = True
                results_by_phase[id(phase)] = PhaseRun(
                    phase=phase.name,
                    task_id=handle.task_id,
                    status="failed",
                    branch=handle.branch,
                    worktree=str(handle.path),
                    session_id=sid,
                    output_digest=digest,
                    error="agent session completed without a valid commit",
                )
                continue
            results_by_phase[id(phase)] = PhaseRun(
                phase=phase.name,
                task_id=handle.task_id,
                status="completed",
                branch=handle.branch,
                worktree=str(handle.path),
                commit=head,
                session_id=sid,
                output_digest=digest,
            )

        if batch_failed:
            blocked = True

    ordered = [results_by_phase[id(phase)] for phase in phases]
    return ParallelRun(run_id=run_id, dry_run=False, phases=ordered)
