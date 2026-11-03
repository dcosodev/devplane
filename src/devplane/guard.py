"""Pre-execution write guard for agent runtimes that support tool hooks.

The post-run check in the pipeline rejects a commit that touched files outside
an assignment's write scopes, but only after the agent has already written
them. This module decides, before a file-writing tool runs, whether the target
is inside the assignment's scopes. It fails closed: any event it cannot parse,
any path it cannot canonicalise, and any target that resolves (through
symlinks) outside the worktree or into a governed root is denied.

Only the file-writing tools are governed. Shell commands can still write files
by other means, so the post-run check remains the authoritative backstop.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import GOVERNED_ROOTS, DevPlaneError
from .scopes import path_in_scope, validate_scope

#: Tool name -> the ``tool_input`` field that carries the target path.
WRITE_TOOLS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
_GOVERNED_FOLDED = frozenset(name.casefold() for name in GOVERNED_ROOTS)


@dataclass(frozen=True)
class GuardPolicy:
    root: Path
    allowed_paths: tuple[str, ...]


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str = ""


def _deny(reason: str) -> GuardDecision:
    return GuardDecision(False, reason)


def decide(policy: GuardPolicy, event: object) -> GuardDecision:
    """Return whether the hook ``event`` may proceed under ``policy``."""
    if not isinstance(event, dict):
        return _deny("malformed hook event")
    tool = event.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return _deny("hook event has no tool name")
    field = WRITE_TOOLS.get(tool)
    if field is None:
        return GuardDecision(True)
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return _deny(f"{tool} event has no tool input")
    target = tool_input.get(field)
    if not isinstance(target, str) or not target:
        return _deny(f"{tool} event has no usable {field}")

    real_root = Path(os.path.realpath(policy.root))
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = real_root / candidate
    resolved = Path(os.path.realpath(candidate))
    if not resolved.is_relative_to(real_root) or resolved == real_root:
        return _deny(f"{target} resolves outside the assignment worktree")
    relative = resolved.relative_to(real_root).as_posix()
    if relative.split("/", 1)[0].casefold() in _GOVERNED_FOLDED:
        return _deny(f"{relative} is a governed path and is read-only")
    try:
        allowed = any(path_in_scope(relative, scope) for scope in policy.allowed_paths)
    except DevPlaneError:
        return _deny(f"{relative} is not a canonical project path")
    if not allowed:
        return _deny(f"{relative} is outside the allowed write scopes")
    return GuardDecision(True)


def load_policy(path: Path) -> GuardPolicy:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DevPlaneError(f"invalid or missing guard policy: {path}") from exc
    if not isinstance(data, dict):
        raise DevPlaneError("guard policy must be a JSON object")
    root = data.get("root")
    scopes = data.get("allowed_paths")
    if not isinstance(root, str) or not os.path.isabs(root):
        raise DevPlaneError("guard policy root must be an absolute path")
    if not isinstance(scopes, list):
        raise DevPlaneError("guard policy allowed_paths must be a list")
    return GuardPolicy(Path(root), tuple(validate_scope(scope) for scope in scopes))


def write_policy(path: Path, policy: GuardPolicy) -> None:
    payload = json.dumps(
        {"root": str(policy.root), "allowed_paths": list(policy.allowed_paths)},
        indent=2,
        sort_keys=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=".guard-", text=True
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload + "\n")
        os.replace(temporary_name, path)
    except OSError as exc:
        raise DevPlaneError("cannot write guard policy") from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
