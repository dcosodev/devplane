"""Agent-neutral runtime adapters for governed DevPlane assignments."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .core import DevPlaneError

MIN_WORKERS = 1
MAX_WORKERS = 8
SUPPORTED_ADAPTERS = ("claude", "hermes", "opencode")


@dataclass(frozen=True)
class AgentRuntimeConfig:
    adapter: str
    executable: str
    provider: str | None
    model: str | None
    options: dict[str, str | int | bool]


@dataclass(frozen=True)
class SessionRequest:
    task_id: str
    prompt: str
    cwd: Path


@dataclass
class SessionResult:
    task_id: str
    returncode: int
    stdout: str
    error: str | None
    stderr: str = ""


class CommandRunner(Protocol):
    def __call__(
        self, args: list[str], cwd: Path
    ) -> subprocess.CompletedProcess[str]: ...


def _string(value: object, field: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise DevPlaneError(f"runtime {field} must be a non-empty string")
    return value


def runtime_from_manifest(resolved: dict[str, Any]) -> AgentRuntimeConfig:
    spec = resolved.get("spec")
    runtime = spec.get("runtime") if isinstance(spec, dict) else None
    if not isinstance(runtime, dict):
        raise DevPlaneError("project has no agent runtime configured")
    legacy_agent = runtime.get("agent")
    adapter = _string(runtime.get("adapter", legacy_agent), "adapter", required=True)
    assert adapter is not None
    if adapter not in SUPPORTED_ADAPTERS:
        raise DevPlaneError(f"unsupported agent adapter: {adapter}")
    executable = _string(runtime.get("executable", adapter), "executable", required=True)
    assert executable is not None
    options = runtime.get("options", {})
    if not isinstance(options, dict) or not all(
        isinstance(key, str) and isinstance(value, (str, int, bool))
        for key, value in options.items()
    ):
        raise DevPlaneError("runtime options must contain scalar values")
    provider = _string(
        runtime.get("provider", "minimax-oauth" if adapter == "hermes" else None),
        "provider",
    )
    model = _string(
        runtime.get("model", "MiniMax-M3" if adapter == "hermes" else None),
        "model",
    )
    return AgentRuntimeConfig(adapter, executable, provider, model, dict(options))


def list_adapters() -> list[str]:
    return list(SUPPORTED_ADAPTERS)


def _hermes_args(request: SessionRequest, config: AgentRuntimeConfig) -> list[str]:
    args = [config.executable, "chat"]
    if config.provider:
        args.extend(["--provider", config.provider])
    if config.model:
        args.extend(["--model", config.model])
    toolsets = config.options.get("toolsets", "terminal,file")
    max_turns = config.options.get("maxTurns", 80)
    if not isinstance(toolsets, str) or not toolsets:
        raise DevPlaneError("Hermes runtime option toolsets must be a string")
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or not 1 <= max_turns <= 500:
        raise DevPlaneError("Hermes runtime option maxTurns must be between 1 and 500")
    args.extend(
        [
            "--toolsets",
            toolsets,
            "--quiet",
            "--max-turns",
            str(max_turns),
            "--source",
            "tool",
            "--query",
            request.prompt,
        ]
    )
    return args


def _claude_args(request: SessionRequest, config: AgentRuntimeConfig) -> list[str]:
    permission_mode = config.options.get("permissionMode", "acceptEdits")
    tools = config.options.get("allowedTools", "Read,Edit,Write,Bash")
    if not isinstance(permission_mode, str) or not isinstance(tools, str):
        raise DevPlaneError("Claude runtime options must be strings")
    args = [
        config.executable,
        "--print",
        "--output-format",
        "text",
        "--no-session-persistence",
        "--permission-mode",
        permission_mode,
        "--allowedTools",
        tools,
    ]
    if config.model:
        args.extend(["--model", config.model])
    args.append(request.prompt)
    return args


def _opencode_args(request: SessionRequest, config: AgentRuntimeConfig) -> list[str]:
    args = [config.executable, "run", "--format", "default"]
    if config.model:
        args.extend(["--model", config.model])
    agent = config.options.get("agent")
    if agent is not None:
        if not isinstance(agent, str) or not agent:
            raise DevPlaneError("OpenCode runtime option agent must be a string")
        args.extend(["--agent", agent])
    args.append(request.prompt)
    return args


def build_agent_args(request: SessionRequest, config: AgentRuntimeConfig) -> list[str]:
    allowed_options = {
        "hermes": {"toolsets", "maxTurns"},
        "claude": {"permissionMode", "allowedTools"},
        "opencode": {"agent"},
    }
    builders = {
        "hermes": _hermes_args,
        "claude": _claude_args,
        "opencode": _opencode_args,
    }
    builder = builders.get(config.adapter)
    if builder is None:
        raise DevPlaneError(f"unsupported agent adapter: {config.adapter}")
    unknown = sorted(set(config.options) - allowed_options[config.adapter])
    if unknown:
        raise DevPlaneError(
            f"unsupported {config.adapter} runtime options: {', '.join(unknown)}"
        )
    if config.adapter != "hermes" and config.provider is not None:
        raise DevPlaneError(
            f"{config.adapter} adapter does not accept provider; encode it in model if required"
        )
    return builder(request, config)


def _validate_workers(max_workers: int) -> None:
    if (
        not isinstance(max_workers, int)
        or isinstance(max_workers, bool)
        or not MIN_WORKERS <= max_workers <= MAX_WORKERS
    ):
        raise DevPlaneError(
            f"max_workers must be between {MIN_WORKERS} and {MAX_WORKERS}: {max_workers}"
        )


def _default_runner() -> CommandRunner:
    from .commands import run_checked

    return run_checked


def _run_one(
    request: SessionRequest,
    runtime: AgentRuntimeConfig,
    runner: CommandRunner,
) -> SessionResult:
    try:
        completed = runner(build_agent_args(request, runtime), request.cwd)
    except DevPlaneError as exc:
        return SessionResult(request.task_id, 1, "", str(exc))
    except FileNotFoundError as exc:
        return SessionResult(
            request.task_id,
            1,
            "",
            f"required executable not found: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - isolate one external runtime
        return SessionResult(
            request.task_id,
            1,
            "",
            f"unexpected runner error: {type(exc).__name__}",
        )
    return SessionResult(
        request.task_id,
        completed.returncode,
        completed.stdout or "",
        None,
        completed.stderr or "",
    )


def run_sessions(
    requests: list[SessionRequest],
    max_workers: int,
    *,
    runtime: AgentRuntimeConfig,
    runner: CommandRunner | None = None,
) -> list[SessionResult]:
    _validate_workers(max_workers)
    if not requests:
        return []
    active_runner = runner or _default_runner()
    results: list[SessionResult | None] = [None] * len(requests)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(requests))) as pool:
        futures = {
            pool.submit(_run_one, request, runtime, active_runner): index
            for index, request in enumerate(requests)
        }
        for future, index in futures.items():
            results[index] = future.result()
    return [result for result in results if result is not None]


__all__ = [
    "AgentRuntimeConfig",
    "SessionRequest",
    "SessionResult",
    "build_agent_args",
    "list_adapters",
    "run_sessions",
    "runtime_from_manifest",
]
