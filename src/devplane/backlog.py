"""Repository-scoped story backlog stored as Markdown files in ``backlog/``.

Each story is one ``backlog/<id>*.md`` file: a YAML frontmatter block followed
by a free-form Markdown body. The files are the source of truth and live in the
repository, so they are reviewed in pull requests and work with any agent.

Frontmatter keys: ``id``, ``title``, ``status``, ``priority``, ``depends_on``,
``write_scope`` and ``feature``. Unknown keys, symlinks, oversized files,
duplicate ids, unknown or cyclic dependencies and scopes outside the supported
glob grammar are rejected rather than interpreted loosely.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .core import GOVERNED_ROOTS, DevPlaneError
from .scopes import scopes_overlap, validate_scope

BACKLOG_DIRECTORY = "backlog"
MAX_STORY_BYTES = 256 * 1024
STATUSES = ("backlog", "ready", "in_progress", "review", "blocked", "done")
PRIORITIES = ("low", "normal", "high", "urgent")
_SCOPE_REQUIRED = frozenset({"ready", "in_progress", "review", "done"})
_OCCUPYING = ("in_progress", "review")
_TRANSITIONS: dict[str, frozenset[str]] = {
    "backlog": frozenset({"ready", "blocked"}),
    "ready": frozenset({"in_progress", "backlog", "blocked"}),
    "in_progress": frozenset({"review", "ready", "blocked"}),
    "review": frozenset({"done", "in_progress", "blocked"}),
    "blocked": frozenset({"backlog", "ready"}),
    "done": frozenset(),
}
_KEYS = frozenset(
    {"id", "title", "status", "priority", "depends_on", "write_scope", "feature"}
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_FEATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FRONTMATTER = re.compile(
    r"\A---[ \t]*\r?\n(?P<front>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)
_STATUS_LINE = re.compile(r"(?m)^status:[ \t]*.*$")
_GOVERNED_FOLDED = frozenset(name.casefold() for name in GOVERNED_ROOTS)


@dataclass(frozen=True)
class Story:
    id: str
    title: str
    status: str
    priority: str
    depends_on: tuple[str, ...]
    write_scope: tuple[str, ...]
    feature: str | None
    body: str
    path: Path = field(compare=False)


@dataclass(frozen=True)
class Backlog:
    stories: tuple[Story, ...]

    def get(self, story_id: str) -> Story:
        for story in self.stories:
            if story.id == story_id:
                return story
        raise DevPlaneError(f"unknown story: {story_id}")


@dataclass(frozen=True)
class Plan:
    """Parallel batches for the ready stories, with the reason for every deferral."""

    batches: tuple[tuple[Story, ...], ...]
    deferred: dict[str, str]
    waiting: dict[str, list[str]]


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _fail(path: Path | str, message: str) -> DevPlaneError:
    return DevPlaneError(f"{path}: {message}")


def _string_list(path: Path, key: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _fail(path, f"{key} must be a list of strings")
    return value


def _parse_text(text: str, path: Path) -> Story:
    match = _FRONTMATTER.match(text)
    if match is None:
        raise _fail(path, "story has no frontmatter block")
    try:
        data = yaml.safe_load(match.group("front"))
    except yaml.YAMLError as exc:
        raise _fail(path, f"invalid frontmatter YAML: {exc}") from exc
    if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
        raise _fail(path, "frontmatter must be a mapping with string keys")
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise _fail(path, f"unknown frontmatter key(s): {', '.join(unknown)}")

    story_id = data.get("id")
    if not isinstance(story_id, str) or _ID.fullmatch(story_id) is None:
        raise _fail(path, f"invalid story id: {story_id!r}")
    if path.stem != story_id and not path.stem.startswith(f"{story_id}-"):
        raise _fail(path, f"file name must start with the story id {story_id}")
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise _fail(path, "title must be a non-empty string")
    status = data.get("status")
    if status not in STATUSES:
        raise _fail(path, f"invalid status: {status!r}")
    priority = data.get("priority", "normal")
    if priority not in PRIORITIES:
        raise _fail(path, f"invalid priority: {priority!r}")
    depends_on = tuple(
        dict.fromkeys(_string_list(path, "depends_on", data.get("depends_on")))
    )
    for dependency in depends_on:
        if _ID.fullmatch(dependency) is None:
            raise _fail(path, f"invalid dependency id: {dependency!r}")
        if dependency == story_id:
            raise _fail(path, "a story cannot depend on itself")
    scopes = _string_list(path, "write_scope", data.get("write_scope"))
    for scope in scopes:
        try:
            validate_scope(scope)
        except DevPlaneError as exc:
            raise _fail(path, f"invalid write_scope entry: {exc}") from exc
        if scope.split("/", 1)[0].casefold() in _GOVERNED_FOLDED:
            raise _fail(path, f"write_scope targets a governed path: {scope}")
    if status in _SCOPE_REQUIRED and not scopes:
        raise _fail(path, f"write_scope is required when status is {status}")
    feature = data.get("feature")
    if feature is not None and (
        not isinstance(feature, str) or _FEATURE.fullmatch(feature) is None
    ):
        raise _fail(path, f"invalid feature: {feature!r}")

    return Story(
        id=story_id,
        title=title.strip(),
        status=status,
        priority=priority,
        depends_on=depends_on,
        write_scope=tuple(dict.fromkeys(scopes)),
        feature=feature,
        body=text[match.end() :],
        path=path,
    )


def _read_story(path: Path) -> Story:
    if path.is_symlink():
        raise _fail(path, "symlinked story files are not allowed")
    try:
        if path.stat().st_size > MAX_STORY_BYTES:
            raise _fail(
                path, f"story file is too large (limit {MAX_STORY_BYTES} bytes)"
            )
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _fail(path, "story file cannot be read as UTF-8") from exc
    return _parse_text(text, path)


def _check_graph(stories: tuple[Story, ...]) -> None:
    by_id: dict[str, Story] = {}
    for story in stories:
        if story.id in by_id:
            raise _fail(
                story.path,
                f"duplicate story id {story.id} (also {by_id[story.id].path.name})",
            )
        by_id[story.id] = story
    for story in stories:
        for dependency in story.depends_on:
            if dependency not in by_id:
                raise _fail(story.path, f"depends on unknown story {dependency}")
    visiting: set[str] = set()
    finished: set[str] = set()

    def visit(story_id: str, trail: list[str]) -> None:
        if story_id in finished:
            return
        if story_id in visiting:
            cycle = " -> ".join([*trail[trail.index(story_id) :], story_id])
            raise DevPlaneError(f"backlog has a dependency cycle: {cycle}")
        visiting.add(story_id)
        for dependency in by_id[story_id].depends_on:
            visit(dependency, [*trail, story_id])
        visiting.discard(story_id)
        finished.add(story_id)

    for story in stories:
        visit(story.id, [])


def load_backlog(project: Path) -> Backlog:
    """Load and validate every story under ``<project>/backlog``."""
    directory = project / BACKLOG_DIRECTORY
    if directory.is_symlink():
        raise _fail(directory, "a symlinked backlog directory is not allowed")
    if not directory.is_dir():
        return Backlog(())
    stories = tuple(
        sorted(
            (_read_story(path) for path in sorted(directory.glob("*.md"))),
            key=lambda story: story.id,
        )
    )
    _check_graph(stories)
    return Backlog(stories)


# ---------------------------------------------------------------------------
# readiness and planning
# ---------------------------------------------------------------------------


def _priority_key(story: Story) -> tuple[int, str]:
    return (-PRIORITIES.index(story.priority), story.id)


def _unfinished_dependencies(backlog: Backlog, story: Story) -> list[str]:
    return sorted(dep for dep in story.depends_on if backlog.get(dep).status != "done")


def ready_stories(backlog: Backlog) -> list[Story]:
    """Ready stories whose dependencies are all done, highest priority first."""
    eligible = [
        story
        for story in backlog.stories
        if story.status == "ready" and not _unfinished_dependencies(backlog, story)
    ]
    return sorted(eligible, key=_priority_key)


def _first_overlap(first: Story, second: Story) -> tuple[str, str] | None:
    for left in first.write_scope:
        for right in second.write_scope:
            if scopes_overlap(left, right):
                return left, right
    return None


def plan_batches(backlog: Backlog, *, max_parallel: int) -> Plan:
    """Group eligible stories into batches that are safe to run in parallel.

    Stories in one batch have pairwise disjoint write scopes. A story whose
    scope overlaps a story already in progress or in review is deferred out of
    every batch until that work is done.
    """
    if (
        not isinstance(max_parallel, int)
        or isinstance(max_parallel, bool)
        or max_parallel < 1
    ):
        raise DevPlaneError("max_parallel must be a positive integer")
    waiting = {
        story.id: unfinished
        for story in backlog.stories
        if story.status == "ready"
        and (unfinished := _unfinished_dependencies(backlog, story))
    }
    occupying = [story for story in backlog.stories if story.status in _OCCUPYING]
    deferred: dict[str, str] = {}
    remaining: list[Story] = []
    for story in ready_stories(backlog):
        for active in occupying:
            overlap = _first_overlap(story, active)
            if overlap is not None:
                state = active.status.replace("_", " ")
                deferred[story.id] = (
                    f"overlaps {active.id} ({state}) on {overlap[0]} vs {overlap[1]}"
                )
                break
        else:
            remaining.append(story)

    batches: list[tuple[Story, ...]] = []
    while remaining:
        batch: list[Story] = []
        later: list[Story] = []
        for story in remaining:
            conflict = next(
                (
                    (member, hit)
                    for member in batch
                    if (hit := _first_overlap(story, member))
                ),
                None,
            )
            if conflict is not None:
                member, (left, right) = conflict
                deferred.setdefault(
                    story.id, f"overlaps {member.id} on {left} vs {right}"
                )
                later.append(story)
            elif len(batch) >= max_parallel:
                deferred.setdefault(
                    story.id, f"parallel limit reached (max_parallel={max_parallel})"
                )
                later.append(story)
            else:
                batch.append(story)
        batches.append(tuple(batch))
        remaining = later
    return Plan(tuple(batches), deferred, waiting)


# ---------------------------------------------------------------------------
# status changes
# ---------------------------------------------------------------------------


def check_transition(old: str, new: str) -> None:
    if old not in _TRANSITIONS or new not in _TRANSITIONS:
        raise DevPlaneError(f"unknown status in transition: {old} -> {new}")
    if new not in _TRANSITIONS[old]:
        raise DevPlaneError(f"invalid status transition: {old} -> {new}")


def set_status(project: Path, story_id: str, new_status: str) -> Story:
    """Change one story's status by rewriting only its frontmatter status line."""
    story = load_backlog(project).get(story_id)
    check_transition(story.status, new_status)
    text = story.path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    if match is None:  # pragma: no cover - load_backlog already parsed this file
        raise _fail(story.path, "story has no frontmatter block")
    front = match.group("front")
    if _STATUS_LINE.search(front) is None:
        raise _fail(story.path, "frontmatter has no status line to update")
    new_front = _STATUS_LINE.sub(f"status: {new_status}", front, count=1)
    updated = text[: match.start("front")] + new_front + text[match.end("front") :]
    result = _parse_text(updated, story.path)  # never leave an invalid story on disk
    _write_atomic(story.path, updated)
    return result


def _write_atomic(path: Path, text: str) -> None:
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=".story-", text=True
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(temporary_name, path.stat().st_mode & 0o777)
        os.replace(temporary_name, path)
    except OSError as exc:
        raise _fail(path, "cannot update the story file") from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
