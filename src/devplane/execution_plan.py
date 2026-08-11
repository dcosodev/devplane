"""Deterministic execution contracts derived from Spec Kit tasks."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .core import DevPlaneError
from .tasks import TaskPhase, build_execution_batches

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SAFE_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")
_PATH_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"((?:(?:\.\.?|[A-Za-z0-9_.-]+)/)*"
    r"(?:[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+/\*\*?))"
)
_GOVERNANCE_ROOTS = {".devplane", ".git", ".hermes", ".specify", "specs"}
_BARE_FILE_SUFFIXES = {
    "bash",
    "c",
    "cc",
    "cfg",
    "conf",
    "cpp",
    "cs",
    "css",
    "csv",
    "ex",
    "exs",
    "gql",
    "go",
    "gradle",
    "graphql",
    "h",
    "hcl",
    "hpp",
    "htm",
    "html",
    "ini",
    "java",
    "js",
    "json",
    "jsx",
    "kt",
    "kts",
    "less",
    "lock",
    "md",
    "php",
    "ps1",
    "proto",
    "py",
    "rb",
    "rs",
    "rst",
    "sass",
    "scala",
    "scss",
    "sh",
    "sql",
    "swift",
    "tf",
    "toml",
    "ts",
    "tsx",
    "tsv",
    "txt",
    "vue",
    "svelte",
    "xml",
    "yaml",
    "yml",
}


@dataclass(frozen=True)
class ExecutionAssignment:
    assignment_id: str
    phase: str
    mode: str
    tasks: tuple[str, ...]
    depends_on: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    validation: tuple[str, ...]
    allow_empty_commit: bool


@dataclass(frozen=True)
class ExecutionPlan:
    run_id: str
    source_tasks: str
    tasks_digest: str
    manifest_digest: str
    base_commit: str
    assignments: tuple[ExecutionAssignment, ...]


def _safe_relative_path(value: str, *, label: str, governance: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise DevPlaneError(f"unsafe {label}: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DevPlaneError(f"unsafe {label}: {value!r}")
    if ":" in value:
        raise DevPlaneError(f"unsafe {label}: {value!r}")
    if governance and path.parts[0] in _GOVERNANCE_ROOTS:
        raise DevPlaneError(f"unsafe {label}: governance path is read-only: {value!r}")
    return path.as_posix()


def _phase_id(index: int, phase: TaskPhase) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", phase.name.lower()).strip("-") or "phase"
    return f"p{index + 1}-{slug}"[:63].rstrip("-")


def _paths_for_phase(phase: TaskPhase) -> tuple[str, ...]:
    paths: set[str] = set()
    unsafe_candidates: list[str] = []
    for task in phase.tasks:
        for match in _PATH_TOKEN.finditer(task.description):
            candidate = match.group(1).rstrip('.,;:)]}"')
            if "/" not in candidate:
                suffix = PurePosixPath(candidate).suffix.removeprefix(".")
                if suffix not in _BARE_FILE_SUFFIXES:
                    continue
            try:
                paths.add(_safe_relative_path(candidate, label="write scope", governance=True))
            except DevPlaneError:
                unsafe_candidates.append(candidate)
    if unsafe_candidates:
        raise DevPlaneError(f"unsafe write scope: {unsafe_candidates[0]!r}")
    if not paths:
        raise DevPlaneError(
            f"phase {phase.name!r} has no writable paths; every assignment requires explicit file boundaries"
        )
    return tuple(sorted(paths))


def _allows_empty_commit(phase: TaskPhase) -> bool:
    phase_name = phase.name.casefold()
    if "foundational" not in phase_name and "validation" not in phase_name:
        return False
    validation_verbs = re.compile(r"^(?:verify|validate|run|confirm|check)\b", re.IGNORECASE)
    return bool(phase.tasks) and all(
        validation_verbs.match(task.description.strip()) for task in phase.tasks
    )


def _scope_prefix(scope: str) -> tuple[tuple[str, ...], bool]:
    parts = PurePosixPath(scope).parts
    prefix: list[str] = []
    wildcard = False
    for part in parts:
        if "*" in part:
            wildcard = True
            break
        prefix.append(part)
    return tuple(prefix), wildcard


def _scopes_overlap(left: str, right: str) -> bool:
    left_prefix, left_wild = _scope_prefix(left)
    right_prefix, right_wild = _scope_prefix(right)
    if not left_wild and not right_wild:
        return left_prefix == right_prefix
    if left_wild and right_wild:
        return left_prefix[: len(right_prefix)] == right_prefix or right_prefix[: len(left_prefix)] == left_prefix
    wildcard_prefix, concrete = (left_prefix, right_prefix) if left_wild else (right_prefix, left_prefix)
    return concrete[: len(wildcard_prefix)] == wildcard_prefix


def _validate_parallel_scopes(assignments: list[ExecutionAssignment]) -> None:
    for index, left in enumerate(assignments):
        for right in assignments[index + 1 :]:
            for left_scope in left.allowed_paths:
                for right_scope in right.allowed_paths:
                    if _scopes_overlap(left_scope, right_scope):
                        raise DevPlaneError(
                            "overlapping write scope in parallel batch: "
                            f"{left.assignment_id}:{left_scope} and {right.assignment_id}:{right_scope}"
                        )


def compute_tasks_digest(phases: list[TaskPhase]) -> str:
    """Hash semantic task content independently from Markdown formatting."""
    canonical_tasks = [
        {
            "phase": phase.name,
            "tasks": [
                {
                    "id": task.task_id,
                    "description": task.description,
                    "parallel": task.parallel,
                    "story": task.story,
                }
                for task in phase.tasks
            ],
        }
        for phase in phases
    ]
    canonical = yaml.safe_dump(canonical_tasks, sort_keys=True, allow_unicode=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def build_execution_plan(
    phases: list[TaskPhase],
    *,
    run_id: str,
    source_tasks: str,
    manifest_digest: str,
    base_commit: str,
    validation_commands: list[str],
) -> ExecutionPlan:
    """Create a deterministic, dependency-aware contract for agent execution."""
    if not _SAFE_ID.fullmatch(run_id):
        raise DevPlaneError(f"run_id must be a safe identifier: {run_id!r}")
    source_tasks = _safe_relative_path(source_tasks, label="tasks source")
    if not _SAFE_COMMIT.fullmatch(base_commit):
        raise DevPlaneError("base_commit must be a hexadecimal Git object id")
    if not isinstance(manifest_digest, str) or not manifest_digest.startswith("sha256:"):
        raise DevPlaneError("manifest_digest must be a sha256 digest")
    if not validation_commands or not all(
        isinstance(command, str) and command.strip() and "\x00" not in command
        for command in validation_commands
    ):
        raise DevPlaneError("at least one non-empty validation command is required")

    phase_ids = {id(phase): _phase_id(index, phase) for index, phase in enumerate(phases)}
    assignments: list[ExecutionAssignment] = []
    previous_batch: tuple[str, ...] = ()

    for batch in build_execution_batches(phases):
        batch_assignments: list[ExecutionAssignment] = []
        mode = "parallel" if len(batch) > 1 else "serial"
        for phase in batch:
            task_ids = tuple(task.task_id for task in phase.tasks)
            assignment = ExecutionAssignment(
                assignment_id=phase_ids[id(phase)],
                phase=phase.name,
                mode=mode,
                tasks=task_ids,
                depends_on=previous_batch,
                allowed_paths=_paths_for_phase(phase),
                validation=tuple(command.strip() for command in validation_commands),
                allow_empty_commit=_allows_empty_commit(phase),
            )
            batch_assignments.append(assignment)
        if len(batch_assignments) > 1:
            _validate_parallel_scopes(batch_assignments)
        assignments.extend(batch_assignments)
        previous_batch = tuple(item.assignment_id for item in batch_assignments)

    return ExecutionPlan(
        run_id=run_id,
        source_tasks=source_tasks,
        tasks_digest=compute_tasks_digest(phases),
        manifest_digest=manifest_digest,
        base_commit=base_commit.lower(),
        assignments=tuple(assignments),
    )


def _as_mapping(plan: ExecutionPlan) -> dict[str, Any]:
    return {
        "apiVersion": "devplane.dev/v1",
        "kind": "ExecutionPlan",
        "metadata": {
            "runId": plan.run_id,
            "sourceTasks": plan.source_tasks,
            "tasksDigest": plan.tasks_digest,
            "manifestDigest": plan.manifest_digest,
            "baseCommit": plan.base_commit,
        },
        "spec": {
            "assignments": [
                {
                    "id": assignment.assignment_id,
                    "phase": assignment.phase,
                    "mode": assignment.mode,
                    "tasks": list(assignment.tasks),
                    "dependsOn": list(assignment.depends_on),
                    "allowedPaths": list(assignment.allowed_paths),
                    "validation": list(assignment.validation),
                    "allowEmptyCommit": assignment.allow_empty_commit,
                }
                for assignment in plan.assignments
            ]
        },
    }


def write_execution_plan(plan: ExecutionPlan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(_as_mapping(plan), sort_keys=False, allow_unicode=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
    digest = f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}\n"
    digest_path = path.with_suffix(path.suffix + ".sha256")
    digest_temporary = digest_path.with_suffix(digest_path.suffix + ".tmp")
    digest_temporary.write_text(digest, encoding="utf-8")
    digest_temporary.replace(digest_path)


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DevPlaneError(f"{label} must be a list of strings")
    return tuple(value)


def load_execution_plan(path: Path) -> ExecutionPlan:
    try:
        payload = path.read_bytes()
        recorded_digest = path.with_suffix(path.suffix + ".sha256").read_text(encoding="utf-8").strip()
        actual_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        if recorded_digest != actual_digest:
            raise DevPlaneError("execution plan integrity check failed")
        data = yaml.safe_load(payload)
    except DevPlaneError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise DevPlaneError(f"invalid execution plan: {path}") from exc
    if not isinstance(data, dict) or data.get("apiVersion") != "devplane.dev/v1" or data.get("kind") != "ExecutionPlan":
        raise DevPlaneError(f"invalid execution plan contract: {path}")
    metadata = data.get("metadata")
    spec = data.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise DevPlaneError(f"invalid execution plan structure: {path}")
    rows = spec.get("assignments")
    if not isinstance(rows, list) or not rows:
        raise DevPlaneError("execution plan assignments must be a non-empty list")
    assignments: list[ExecutionAssignment] = []
    for row in rows:
        if not isinstance(row, dict):
            raise DevPlaneError("execution plan assignment must be a mapping")
        allow_empty_commit = row.get("allowEmptyCommit", False)
        if not isinstance(allow_empty_commit, bool):
            raise DevPlaneError("assignment.allowEmptyCommit must be a boolean")
        assignments.append(
            ExecutionAssignment(
                assignment_id=str(row.get("id", "")),
                phase=str(row.get("phase", "")),
                mode=str(row.get("mode", "")),
                tasks=_string_list(row.get("tasks"), "assignment.tasks"),
                depends_on=_string_list(row.get("dependsOn"), "assignment.dependsOn"),
                allowed_paths=_string_list(row.get("allowedPaths"), "assignment.allowedPaths"),
                validation=_string_list(row.get("validation"), "assignment.validation"),
                allow_empty_commit=allow_empty_commit,
            )
        )
    plan = ExecutionPlan(
        run_id=str(metadata.get("runId", "")),
        source_tasks=str(metadata.get("sourceTasks", "")),
        tasks_digest=str(metadata.get("tasksDigest", "")),
        manifest_digest=str(metadata.get("manifestDigest", "")),
        base_commit=str(metadata.get("baseCommit", "")),
        assignments=tuple(assignments),
    )
    # Re-validate the fields that can lead to filesystem or Git operations.
    if not _SAFE_ID.fullmatch(plan.run_id):
        raise DevPlaneError("execution plan contains an unsafe run id")
    _safe_relative_path(plan.source_tasks, label="tasks source")
    if not _SAFE_COMMIT.fullmatch(plan.base_commit):
        raise DevPlaneError("execution plan contains an unsafe base commit")
    known: set[str] = set()
    known_tasks: set[str] = set()
    parallel_groups: dict[tuple[str, ...], list[ExecutionAssignment]] = {}
    for assignment in plan.assignments:
        if not _SAFE_ID.fullmatch(assignment.assignment_id) or assignment.assignment_id in known:
            raise DevPlaneError("execution plan contains an invalid or duplicate assignment id")
        if assignment.mode not in {"serial", "parallel"}:
            raise DevPlaneError("execution plan contains an invalid assignment mode")
        if not assignment.phase or any(character in assignment.phase for character in "\x00\r\n"):
            raise DevPlaneError("execution plan contains an invalid phase")
        if not assignment.tasks or any(not task.strip() for task in assignment.tasks):
            raise DevPlaneError("execution plan assignment requires tasks")
        if known_tasks.intersection(assignment.tasks):
            raise DevPlaneError("execution plan assigns a task more than once")
        known_tasks.update(assignment.tasks)
        if any(dependency not in known for dependency in assignment.depends_on):
            raise DevPlaneError("execution plan contains an unknown or forward dependency")
        if not assignment.allowed_paths:
            raise DevPlaneError("execution plan assignment requires allowed paths")
        for scope in assignment.allowed_paths:
            _safe_relative_path(scope, label="write scope", governance=True)
        if not assignment.validation or any(
            not command.strip() or any(character in command for character in "\x00\r\n")
            for command in assignment.validation
        ):
            raise DevPlaneError("execution plan assignment requires safe validation commands")
        if assignment.mode == "parallel":
            parallel_groups.setdefault(assignment.depends_on, []).append(assignment)
        known.add(assignment.assignment_id)
    for group in parallel_groups.values():
        if len(group) > 1:
            _validate_parallel_scopes(group)
    return plan
