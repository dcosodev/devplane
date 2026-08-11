from __future__ import annotations

import re
from pathlib import Path

from .core import DevPlaneError, build_resolved_manifest

_COMMAND_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")


def _validate_command_name(command: str) -> None:
    if not _COMMAND_NAME.fullmatch(command):
        raise DevPlaneError(f"invalid context command name: {command!r}")


def build_context_bundle(project_root: Path, command: str) -> str:
    _validate_command_name(command)
    project_root = project_root.resolve()
    resolved = build_resolved_manifest(project_root)
    command_cfg = resolved["spec"]["commands"].get(command)
    if command_cfg is None:
        raise DevPlaneError(f"no context configured for command: {command}")
    lines = [
        "# DevPlane context bundle",
        "",
        f"Command: {command}",
        f"Manifest: {resolved['metadata']['sourceHash']}",
        "",
        "## Included resources",
    ]
    catalog_root = (project_root / resolved["spec"]["catalog"]["source"]).resolve()
    for item in command_cfg["context"]:
        resource = (catalog_root / item["source"]).resolve()
        if not resource.is_relative_to(catalog_root):
            raise DevPlaneError(f"context resource escapes catalog: {item['source']}")
        lines.extend([f"- {item['path']} ({item['reason']})", "", resource.read_text(encoding="utf-8").rstrip(), ""])
    lines.extend(["## Write scopes"])
    lines.extend(f"- {scope}" for scope in command_cfg["writeScopes"])
    lines.extend(["", "## Allowed shell commands"])
    lines.extend(f"- {pattern}" for pattern in command_cfg["shellAllow"])
    return "\n".join(lines).rstrip() + "\n"


def write_context_bundle(project_root: Path, command: str) -> Path:
    _validate_command_name(command)
    output = project_root.resolve() / ".devplane" / "generated" / f"context-{command}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_context_bundle(project_root, command), encoding="utf-8")
    return output
