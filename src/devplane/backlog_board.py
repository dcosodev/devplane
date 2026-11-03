"""A column view of the backlog, with what the planner will do with each ready story."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .backlog import PRIORITIES, STATUSES, Backlog, Story, plan_batches


def _sort_key(story: Story) -> tuple[int, str]:
    return (-PRIORITIES.index(story.priority), story.id)


def _ready_notes(backlog: Backlog, max_parallel: int) -> tuple[dict[str, str], list[str]]:
    plan = plan_batches(backlog, max_parallel=max_parallel)
    notes: dict[str, str] = {}
    for story_id, unfinished in plan.waiting.items():
        notes[story_id] = f"waits for {', '.join(unfinished)}"
    for index, batch in enumerate(plan.batches, start=1):
        for story in batch:
            reason = plan.deferred.get(story.id)
            if index == 1:
                notes[story.id] = "runs next"
            else:
                notes[story.id] = f"wave {index}: {reason}" if reason else f"wave {index}"
    for story_id, reason in plan.deferred.items():
        notes.setdefault(story_id, f"deferred: {reason}")
    next_batch = [story.id for story in plan.batches[0]] if plan.batches else []
    return notes, next_batch


def _note(story: Story, ready_notes: dict[str, str]) -> str:
    if story.status == "ready":
        return ready_notes.get(story.id, "")
    if story.status == "backlog" and not story.write_scope:
        return "no write_scope yet"
    return ""


def build_board(
    backlog: Backlog,
    *,
    max_parallel: int,
    feature_statuses: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """Group stories by status; ready cards say whether and when the planner runs them."""
    ready_notes, next_batch = _ready_notes(backlog, max_parallel)
    columns: dict[str, list[dict[str, Any]]] = {status: [] for status in STATUSES}
    for story in sorted(backlog.stories, key=_sort_key):
        columns[story.status].append(
            {
                "id": story.id,
                "title": story.title,
                "priority": story.priority,
                "depends_on": list(story.depends_on),
                "write_scope": list(story.write_scope),
                "feature": story.feature,
                "feature_status": (feature_statuses or {}).get(story.feature) if story.feature else None,
                "note": _note(story, ready_notes),
            }
        )
    return {"columns": columns, "next_batch": next_batch}


def _heading(status: str) -> str:
    return status.replace("_", " ")


def render_text(board: dict[str, Any]) -> str:
    lines: list[str] = []
    for status, cards in board["columns"].items():
        lines.append(f"{_heading(status).upper()} ({len(cards)})")
        for card in cards:
            suffix = f"  — {card['note']}" if card["note"] else ""
            if card["feature"]:
                status_part = f": {card['feature_status']}" if card["feature_status"] else ""
                suffix += f"  [feature {card['feature']}{status_part}]"
            lines.append(f"  {card['id']:<8}{card['priority']:<8}{card['title']}{suffix}")
    next_batch = ", ".join(board["next_batch"]) or "none"
    lines.append("")
    lines.append(f"next batch: {next_batch}")
    return "\n".join(lines) + "\n"


def _cell(value: str) -> str:
    return value.replace("|", "\\|")


def render_markdown(board: dict[str, Any]) -> str:
    blocks: list[str] = []
    for status, cards in board["columns"].items():
        block = [f"### {_heading(status).capitalize()} ({len(cards)})", ""]
        if cards:
            block += ["| ID | Priority | Title | Note |", "| --- | --- | --- | --- |"]
            block += [
                f"| {card['id']} | {card['priority']} | {_cell(card['title'])} | {_cell(card['note'])} |"
                for card in cards
            ]
        else:
            block.append("_none_")
        blocks.append("\n".join(block))
    blocks.append("Next batch: " + (", ".join(board["next_batch"]) or "none"))
    return "\n\n".join(blocks) + "\n"
