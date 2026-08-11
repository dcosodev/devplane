from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml


class DevPlaneError(RuntimeError):
    """A deterministic configuration or validation failure."""


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DevPlaneError(f"required file not found: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DevPlaneError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DevPlaneError(f"expected YAML mapping in {path}")
    return value


def _require_kind(data: dict[str, Any], kind: str, path: Path) -> None:
    if data.get("apiVersion") != "devplane.dev/v1" or data.get("kind") != kind:
        raise DevPlaneError(f"{path} must be devplane.dev/v1 kind {kind}")


def _safe_child(root: Path, candidate: Path, label: str) -> Path:
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise DevPlaneError(f"{label} escapes allowed root {resolved_root}: {candidate}")
    return resolved


def _parse_capability_request(value: str) -> tuple[str, str | None]:
    if "@" not in value:
        return value, None
    capability_id, version = value.rsplit("@", 1)
    if not capability_id or not version:
        raise DevPlaneError(f"invalid capability request: {value}")
    return capability_id, version


def _canonical_yaml(data: dict[str, Any]) -> bytes:
    return yaml.safe_dump(data, sort_keys=True, allow_unicode=True).encode("utf-8")


def _source_hash(files: list[Path], logical_names: list[str]) -> str:
    digest = hashlib.sha256()
    for logical, path in sorted(zip(logical_names, files), key=lambda pair: pair[0]):
        digest.update(logical.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def build_resolved_manifest(project_root: Path) -> dict[str, Any]:
    project_root = project_root.resolve()
    config_path = project_root / ".devplane" / "project.yaml"
    project = _load_yaml(config_path)
    _require_kind(project, "AgentProject", config_path)
    spec = project.get("spec")
    if not isinstance(spec, dict):
        raise DevPlaneError("AgentProject.spec must be a mapping")

    catalog_cfg = spec.get("catalog")
    if not isinstance(catalog_cfg, dict) or not isinstance(catalog_cfg.get("source"), str):
        raise DevPlaneError("spec.catalog.source is required")
    catalog_root = _safe_child(
        project_root.parent,
        project_root / catalog_cfg["source"],
        "catalog source",
    )
    catalog_path = catalog_root / "manifest.yaml"
    catalog = _load_yaml(catalog_path)
    _require_kind(catalog, "CapabilityCatalog", catalog_path)

    entries = catalog.get("spec", {}).get("capabilities", [])
    if not isinstance(entries, list):
        raise DevPlaneError("CapabilityCatalog.spec.capabilities must be a list")
    available: dict[str, tuple[dict[str, Any], Path]] = {}
    source_files = [config_path, catalog_path]
    source_names = ["project.yaml", "catalog/manifest.yaml"]
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("ref"), str):
            raise DevPlaneError("catalog capability entries require a string ref")
        cap_path = _safe_child(catalog_root, catalog_root / entry["ref"], "capability ref")
        cap = _load_yaml(cap_path)
        _require_kind(cap, "Capability", cap_path)
        metadata = cap.get("metadata", {})
        cap_id = metadata.get("id")
        if not isinstance(cap_id, str) or not cap_id:
            raise DevPlaneError(f"capability has no metadata.id: {cap_path}")
        if cap_id in available:
            raise DevPlaneError(f"duplicate capability id: {cap_id}")
        available[cap_id] = (cap, cap_path)
        source_files.append(cap_path)
        source_names.append(f"catalog/{cap_path.relative_to(catalog_root).as_posix()}")

    requested = spec.get("capabilities", [])
    if not isinstance(requested, list) or not all(isinstance(item, str) for item in requested):
        raise DevPlaneError("spec.capabilities must be a list of strings")

    active: list[dict[str, Any]] = []
    commands: dict[str, dict[str, Any]] = {}
    for request in requested:
        cap_id, pinned = _parse_capability_request(request)
        if cap_id not in available:
            raise DevPlaneError(f"unknown capability: {cap_id}")
        cap, cap_path = available[cap_id]
        version = str(cap.get("metadata", {}).get("version", ""))
        if pinned is not None and pinned != version:
            raise DevPlaneError(f"capability {cap_id} requested {pinned}, catalog has {version}")
        active.append({"id": cap_id, "resolvedVersion": version, "origin": cap_path.relative_to(catalog_root).as_posix()})
        cap_spec = cap.get("spec", {})
        contexts = cap_spec.get("context", {}) if isinstance(cap_spec, dict) else {}
        permissions = cap_spec.get("permissions", {}) if isinstance(cap_spec, dict) else {}
        if contexts is None:
            contexts = {}
        if not isinstance(contexts, dict) or not isinstance(permissions, dict):
            raise DevPlaneError(f"invalid context or permissions in capability {cap_id}")
        for command, context_cfg in contexts.items():
            if not isinstance(command, str) or not isinstance(context_cfg, dict):
                raise DevPlaneError(f"invalid context declaration in capability {cap_id}")
            includes = context_cfg.get("include", [])
            if not isinstance(includes, list) or not all(isinstance(item, str) for item in includes):
                raise DevPlaneError(f"context include for {cap_id}/{command} must be strings")
            target = commands.setdefault(command, {"context": [], "writeScopes": [], "shellAllow": []})
            for include in includes:
                resource = _safe_child(cap_path.parent, cap_path.parent / include, "context resource")
                if not resource.is_file():
                    raise DevPlaneError(f"context resource is not a file: {resource}")
                logical = f"{cap_id}/{include}"
                catalog_relative = resource.relative_to(catalog_root).as_posix()
                target["context"].append({"path": logical, "source": catalog_relative, "reason": f"capability {cap_id}"})
                source_files.append(resource)
                source_names.append(f"catalog/{cap_path.parent.relative_to(catalog_root).as_posix()}/{include}")
            write = permissions.get("write", [])
            shell = permissions.get("shell", {}).get("allow", []) if isinstance(permissions.get("shell", {}), dict) else []
            if not isinstance(write, list) or not all(isinstance(item, str) for item in write):
                raise DevPlaneError(f"permissions.write for {cap_id} must be strings")
            if not isinstance(shell, list) or not all(isinstance(item, str) for item in shell):
                raise DevPlaneError(f"permissions.shell.allow for {cap_id} must be strings")
            target["writeScopes"].extend(write)
            target["shellAllow"].extend(shell)

    for command in commands.values():
        command["context"] = sorted(command["context"], key=lambda item: item["path"])
        command["writeScopes"] = sorted(set(command["writeScopes"]))
        command["shellAllow"] = sorted(set(command["shellAllow"]))

    workflow = spec.get("workflow", {})
    runtime = spec.get("runtime", {})
    if not isinstance(workflow, dict) or workflow.get("engine") != "speckit":
        raise DevPlaneError("MVP requires spec.workflow.engine: speckit")
    if not isinstance(runtime, dict) or runtime.get("agent") != "hermes":
        raise DevPlaneError("MVP requires spec.runtime.agent: hermes")

    repository_profile = spec.get("repositoryProfile")
    if repository_profile is not None:
        if not isinstance(repository_profile, dict):
            raise DevPlaneError("spec.repositoryProfile must be a mapping")
        digest = repository_profile.get("digest")
        if (
            repository_profile.get("approved") is not True
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or len(digest.removeprefix("sha256:")) != 64
        ):
            raise DevPlaneError("spec.repositoryProfile requires an approved sha256 digest")
        for key in (
            "languages",
            "packageManagers",
            "frameworks",
            "architecture",
            "validation",
            "evidence",
        ):
            value = repository_profile.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise DevPlaneError(f"spec.repositoryProfile.{key} must be a list of strings")

    return {
        "apiVersion": "devplane.dev/v1",
        "kind": "ResolvedManifest",
        "metadata": {
            "project": project.get("metadata", {}).get("name", project_root.name),
            "sourceHash": _source_hash(source_files, source_names),
        },
        "spec": {
            "catalog": {"name": catalog.get("metadata", {}).get("name"), "source": catalog_cfg["source"]},
            "workflow": {"engine": "speckit", "integration": "hermes"},
            "runtime": {"agent": "hermes"},
            "repositoryProfile": repository_profile,
            "activeCapabilities": sorted(active, key=lambda item: item["id"]),
            "commands": dict(sorted(commands.items())),
        },
    }


def sync_project(project_root: Path, check: bool = False) -> dict[str, Any]:
    resolved = build_resolved_manifest(project_root)
    output = project_root.resolve() / ".devplane" / "generated" / "resolved-manifest.yaml"
    expected = _canonical_yaml(resolved)
    if check:
        if not output.is_file() or output.read_bytes() != expected:
            raise DevPlaneError(f"generated manifest drift detected: {output}")
        return resolved
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(expected)
    return resolved
