"""Deterministic repository stack discovery from local evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .core import DevPlaneError

_IGNORED_PARTS = {".git", ".venv", "node_modules", ".devplane", ".specify"}


def _exists(project: Path, relative: str) -> bool:
    return (project / relative).is_file()


def discover_repository(project: Path) -> dict[str, Any]:
    project = project.expanduser().resolve()
    evidence: list[str] = []
    languages: set[str] = set()
    managers: set[str] = set()
    frameworks: set[str] = set()
    validation: list[str] = []

    markers = {
        "pyproject.toml": ("python", None),
        "requirements.txt": ("python", "pip"),
        "uv.lock": ("python", "uv"),
        "package.json": ("javascript", None),
        "pnpm-lock.yaml": ("javascript", "pnpm"),
        "yarn.lock": ("javascript", "yarn"),
        "package-lock.json": ("javascript", "npm"),
        "go.mod": ("go", "go"),
        "Cargo.toml": ("rust", "cargo"),
        "Gemfile": ("ruby", "bundler"),
        "pom.xml": ("java", "maven"),
        "build.gradle": ("java", "gradle"),
    }
    for marker, (language, manager) in markers.items():
        if _exists(project, marker):
            evidence.append(marker)
            languages.add(language)
            if manager:
                managers.add(manager)

    pyproject = project / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace").lower()
        for dependency, framework in (
            ("fastapi", "fastapi"),
            ("django", "django"),
            ("flask", "flask"),
            ("pytest", "pytest"),
        ):
            if dependency in text:
                frameworks.add(framework)
        if "pytest" in text:
            validation.append("uv run pytest" if "uv" in managers else "python -m pytest")

    package = project / "package.json"
    if package.is_file():
        try:
            package_data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DevPlaneError("invalid package.json during repository discovery") from exc
        dependencies: dict[str, object] = {}
        scripts: dict[str, object] = {}
        if isinstance(package_data, dict):
            for key in ("dependencies", "devDependencies"):
                value = package_data.get(key, {})
                if isinstance(value, dict):
                    dependencies.update(value)
            raw_scripts = package_data.get("scripts", {})
            if isinstance(raw_scripts, dict):
                scripts = raw_scripts
        for dependency, framework in (
            ("react", "react"),
            ("next", "nextjs"),
            ("vue", "vue"),
            ("@angular/core", "angular"),
            ("express", "express"),
            ("nestjs", "nestjs"),
        ):
            if dependency in dependencies:
                frameworks.add(framework)
        manager = "pnpm" if "pnpm" in managers else "yarn" if "yarn" in managers else "npm"
        managers.add(manager)
        for script in ("test", "lint", "typecheck", "check"):
            if script in scripts:
                validation.append(f"{manager} {script}" if script == "test" else f"{manager} run {script}")

    if "go" in languages:
        validation.append("go test ./...")
    if "rust" in languages:
        validation.append("cargo test")

    architecture = sorted(
        name
        for name in ("src", "tests", "app", "backend", "frontend", "services", "packages", "cmd", "internal")
        if (project / name).is_dir()
    )
    profile: dict[str, Any] = {
        "apiVersion": "devplane.dev/v1",
        "kind": "RepositoryProfile",
        "status": "existing" if evidence else "greenfield",
        "languages": sorted(languages),
        "packageManagers": sorted(managers),
        "frameworks": sorted(frameworks),
        "architecture": architecture,
        "validation": list(dict.fromkeys(validation)),
        "evidence": sorted(evidence),
    }
    canonical = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
    profile["digest"] = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    return profile


def write_repository_profile(project: Path, profile: dict[str, Any]) -> Path:
    path = project.expanduser().resolve() / ".git" / "devplane" / "repository-profile.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    return path
