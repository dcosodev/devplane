"""Parser and scheduler for GitHub Spec Kit ``tasks.md`` documents.

Spec Kit emits a Markdown checklist organised in ``## <phase>`` sections,
each holding ``- [ ] TNNN`` task lines that may be flagged with ``[P]`` to
indicate the task may run in parallel, and ``[USn]`` to identify the user
story a task belongs to. This module turns that text into typed phase/task
structures and splits the phases into the execution batches that the
Hermes runtime consumes: ``Setup``/``Foundational``/``Polish`` phases
must run serially, while consecutive ``User Story`` phases may run in
parallel as a single batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Phases that must always run serially, never merged with anything else.
_SERIAL_PHASES = {"setup", "foundational", "polish"}


@dataclass(frozen=True)
class SpecTask:
    """A single ``- [ ] TNNN`` line parsed out of a ``tasks.md`` file."""

    task_id: str
    description: str
    parallel: bool
    story: str | None
    phase: str


@dataclass(frozen=True)
class TaskPhase:
    """An ``## <phase>`` section with its parsed tasks."""

    name: str
    tasks: list[SpecTask] = field(default_factory=list)


def _is_phase_heading(line: str) -> str | None:
    """Return the phase name if ``line`` is an ``## `` heading, else None."""

    stripped = line.strip()
    if not stripped.startswith("## "):
        return None
    # Reject deeper headings (###, ####, ...).
    body = stripped[3:]
    if body.startswith("#"):
        return None
    return body.strip()


def _classify_phase(name: str) -> str:
    """Return the canonical bucket a phase belongs to.

    The detection is substring-based (case-insensitive) so it tolerates the
    variations Spec Kit emits: bare ``"User Story 1"``, colon-prefixed
    ``"Phase 3: User Story 1"`` and equivalent forms.
    """

    lowered = name.lower()
    if "user story" in lowered:
        return "user story"
    head = lowered.split(":", 1)[0].strip()
    return head


def _parse_task_line(line: str, phase: str) -> SpecTask | None:
    """Parse ``- [ ] TNNN [P]? [USn]? description`` into a ``SpecTask``.

    Returns ``None`` for completed (``- [x]``) lines or other non-task
    bullets. Raises ``ValueError`` for malformed active task lines so
    callers can pinpoint the offending line number.
    """

    stripped = line.strip()
    if not stripped.startswith(("- [ ]", "*[ ]", "* [ ]")):
        return None
    if stripped.startswith(("- [x]", "- [X]")):
        return None

    # Reconstruct the part after the checkbox marker.
    # We support the canonical form "- [ ] T001 description".
    if not stripped.startswith("- [ ]"):
        # "* [ ]" or other Markdown bullets are treated as non-task noise.
        return None
    payload = stripped[len("- [ ]") :].strip()
    if not payload:
        raise ValueError(f"invalid task line (empty after checkbox): {line!r}")

    tokens = payload.split()
    if not tokens:
        raise ValueError(f"invalid task line (no tokens): {line!r}")
    task_id = tokens[0]
    # Spec Kit task ids are uppercase "T" followed by digits.
    if not (len(task_id) >= 2 and task_id[0] == "T" and task_id[1:].isdigit()):
        raise ValueError(f"invalid task line (bad task id {task_id!r}): {line!r}")

    parallel = False
    story: str | None = None
    description_tokens: list[str] = []

    for token in tokens[1:]:
        if token == "[P]":
            parallel = True
            continue
        if token.startswith("[") and token.endswith("]") and len(token) >= 3:
            inner = token[1:-1]
            if inner.startswith("US") and inner[2:].isdigit():
                story = inner
                continue
        description_tokens.append(token)

    description = " ".join(description_tokens).strip()
    if not description:
        raise ValueError(f"invalid task line (missing description): {line!r}")

    return SpecTask(
        task_id=task_id,
        description=description,
        parallel=parallel,
        story=story,
        phase=phase,
    )


def parse_tasks_markdown(text: str) -> list[TaskPhase]:
    """Parse a ``tasks.md`` body into ``TaskPhase`` objects.

    Rules:
      * ``## <name>`` headings start a new phase (deeper headings ignored).
      * ``- [ ]`` lines inside a phase become ``SpecTask`` rows.
      * ``- [x]`` lines are ignored.
      * Task IDs must be unique across the whole document.
      * Any ``- [ ]`` line appearing before the first ``## `` heading is
        rejected as orphan input.
      * Malformed task lines raise ``ValueError`` with a precise message.

    Raises ``ValueError`` when no tasks are present anywhere in the input.
    """

    if not text or not text.strip():
        raise ValueError("no tasks found in tasks.md (empty input)")

    phases: list[TaskPhase] = []
    seen_ids: set[str] = set()
    orphan: tuple[int, str] | None = None

    current_name: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        heading = _is_phase_heading(line)
        if heading is not None:
            current_name = heading
            phases.append(TaskPhase(name=heading))
            continue

        stripped = line.strip()
        if not stripped:
            continue

        # Ignore lines that are clearly not Spec Kit checklist rows. We
        # only react to lines that look like task bullets.
        if not stripped.startswith(("- [ ]", "- [x]", "- [X]")):
            continue

        if current_name is None:
            orphan = (line_number, stripped)
            break

        if stripped.startswith(("- [x]", "- [X]")):
            continue

        task = _parse_task_line(line, current_name)
        if task is None:
            continue
        if task.task_id in seen_ids:
            raise ValueError(f"duplicate task id: {task.task_id}")
        seen_ids.add(task.task_id)
        phases[-1].tasks.append(task)

    if orphan is not None:
        line_number, content = orphan
        raise ValueError(
            f"no phase context for task line at offset {line_number}: {content!r}"
        )

    populated = [phase for phase in phases if phase.tasks]
    if not populated:
        raise ValueError("no tasks found in tasks.md (expected at least one '- [ ] TNNN' entry)")
    return populated


def build_execution_batches(phases: list[TaskPhase]) -> list[list[TaskPhase]]:
    """Group phases into execution batches honouring the Spec Kit contract.

    ``Setup``, ``Foundational`` and ``Polish`` phases run alone in their own
    batch. ``User Story`` phases (any phase whose leading word matches
    ``User Story``) that appear consecutively are merged into a single
    batch so the runtime can dispatch them in parallel; any serial phase
    that sits between two user-story phases breaks that grouping.
    """

    batches: list[list[TaskPhase]] = []
    current: list[TaskPhase] = []

    def _flush() -> None:
        nonlocal current
        if current:
            batches.append(current)
            current = []

    for phase in phases:
        kind = _classify_phase(phase.name)
        if kind in _SERIAL_PHASES:
            # Serial phases always close the current batch and start their
            # own; they never merge with anything, including each other.
            _flush()
            batches.append([phase])
            continue

        if kind == "user story":
            if current and _classify_phase(current[-1].name) != "user story":
                # A serial phase sits between us and the previous user
                # story batch: close it so this one starts fresh.
                _flush()
            current.append(phase)
            continue

        # Unknown phase kinds are treated as serial to keep the runtime
        # deterministic; we never invent parallel semantics we cannot name.
        _flush()
        batches.append([phase])

    _flush()
    return batches