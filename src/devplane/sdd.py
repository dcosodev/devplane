"""Governed Spec Kit phase execution and local feature state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from .core import DevPlaneError

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_PHASES = {"specify", "plan", "tasks"}


@dataclass(frozen=True)
class FeatureState:
    run_id: str
    status: str
    manifest_digest: str
    spec_digest: str
    external_runs: dict[str, str]

    def to_mapping(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "manifest_digest": self.manifest_digest,
            "spec_digest": self.spec_digest,
            "external_runs": dict(sorted(self.external_runs.items())),
        }


def _state_dir(project: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not _SAFE_ID.fullmatch(run_id):
        raise DevPlaneError("run_id must be a safe identifier")
    return project.expanduser().resolve() / ".git" / "devplane" / "runs" / run_id


def write_phase_workflow(project: Path, run_id: str, phase: str) -> Path:
    """Materialize a one-phase Spec Kit prompt workflow without vendoring upstream."""
    if phase not in _PHASES:
        raise DevPlaneError(f"unsupported Spec Kit phase: {phase}")
    path = _state_dir(project, run_id) / f"phase-{phase}.yaml"
    data = {
        "schema_version": "1.0",
        "workflow": {
            "id": f"devplane-{phase}",
            "name": f"DevPlane {phase}",
            "version": "1.0.0",
            "description": f"Invoke only /speckit-{phase} under DevPlane gates",
        },
        "requires": {"speckit_version": ">=0.8.5"},
        "inputs": {
            "spec": {"type": "string", "required": True},
            "integration": {"type": "string", "default": "hermes"},
        },
        "steps": [
            {
                "id": phase,
                "type": "prompt",
                "prompt": f"/speckit-{phase} {{{{ inputs.spec }}}}",
                "integration": "{{ inputs.integration }}",
                "model": "MiniMax-M3",
                "timeout": 900,
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def write_feature_state(project: Path, state: FeatureState) -> None:
    directory = _state_dir(project, state.run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "feature-state.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state.to_mapping(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_feature_state(project: Path, run_id: str) -> FeatureState:
    path = _state_dir(project, run_id) / "feature-state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DevPlaneError(f"invalid or missing feature state: {path}") from exc
    if not isinstance(data, dict) or data.get("run_id") != run_id:
        raise DevPlaneError("feature state identity mismatch")
    external = data.get("external_runs")
    if not isinstance(external, dict) or not all(
        isinstance(key, str) and key in _PHASES and isinstance(value, str)
        for key, value in external.items()
    ):
        raise DevPlaneError("invalid external run mapping in feature state")
    status = data.get("status")
    manifest = data.get("manifest_digest")
    spec_digest = data.get("spec_digest")
    if not isinstance(status, str) or not status:
        raise DevPlaneError("invalid feature state status")
    if not isinstance(manifest, str) or not manifest:
        raise DevPlaneError("invalid feature state manifest digest")
    if not isinstance(spec_digest, str) or not spec_digest:
        raise DevPlaneError("invalid feature state spec digest")
    return FeatureState(run_id, status, manifest, spec_digest, dict(external))


def write_feature_request(project: Path, run_id: str, spec: str) -> None:
    if not isinstance(spec, str) or not spec.strip():
        raise DevPlaneError("feature description must not be empty")
    path = _state_dir(project, run_id) / "request.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=".request-",
            text=True,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(spec)
        os.replace(temporary_name, path)
    except OSError as exc:
        raise DevPlaneError("cannot protect local feature request") from exc
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def read_feature_request(project: Path, run_id: str, expected_digest: str) -> str:
    path = _state_dir(project, run_id) / "request.txt"
    try:
        spec = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DevPlaneError("feature request is unavailable") from exc
    digest = f"sha256:{hashlib.sha256(spec.encode('utf-8')).hexdigest()}"
    if digest != expected_digest:
        raise DevPlaneError("feature request digest mismatch")
    return spec


class CommandRunner(Protocol):
    def __call__(
        self,
        args: list[str],
        cwd: Path,
        *,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]: ...


def _completed_phase_payload(stdout: str, phase: str) -> dict[str, object]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    for candidate in [stdout, *reversed(lines)]:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("run_id"), str)
            and bool(payload["run_id"])
            and payload.get("status") == "completed"
        ):
            return payload
    raise DevPlaneError(f"invalid Spec Kit JSON output for {phase}")


def execute_phase(
    project: Path,
    run_id: str,
    phase: str,
    spec: str,
    *,
    command_runner: CommandRunner,
) -> dict[str, object]:
    workflow = write_phase_workflow(project, run_id, phase)
    environment = os.environ.copy()
    environment["SPECKIT_INTEGRATION_HERMES_EXTRA_ARGS"] = "--provider minimax-oauth"
    result = command_runner(
        [
            "specify",
            "workflow",
            "run",
            str(workflow),
            "--input",
            f"spec={spec}",
            "--input",
            "integration=hermes",
            "--json",
        ],
        project.expanduser().resolve(),
        environment=environment,
    )
    payload = _completed_phase_payload(result.stdout or "", phase)
    external_run_id = payload.get("run_id")
    status = payload.get("status")
    if not isinstance(external_run_id, str) or not external_run_id:
        raise DevPlaneError(f"Spec Kit {phase} returned no run_id")
    if status != "completed":
        raise DevPlaneError(f"Spec Kit {phase} did not complete successfully")
    return payload
