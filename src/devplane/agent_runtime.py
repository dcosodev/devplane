"""Agent-neutral runtime adapters for governed DevPlane assignments."""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .core import SUPPORTED_ADAPTERS, DevPlaneError
from .guard import HERMES_WRITE_TOOLS

MIN_WORKERS = 1
MAX_WORKERS = 8


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
    guard_policy: Path | None = None


@dataclass
class SessionResult:
    task_id: str
    returncode: int
    stdout: str
    error: str | None
    stderr: str = ""
    usage: dict[str, float] | None = None


class CommandRunner(Protocol):
    def __call__(
        self, args: list[str], cwd: Path, *, environment: Mapping[str, str] | None = None
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
    provider = _string(runtime.get("provider"), "provider")
    model = _string(runtime.get("model"), "model")
    return AgentRuntimeConfig(adapter, executable, provider, model, dict(options))


def list_adapters() -> list[str]:
    return list(SUPPORTED_ADAPTERS)


GUARD_POLICY_ENV = "DEVPLANE_GUARD_POLICY"
#: How each adapter can enforce a write scope: before a file is written, or only by the
#: post-run allowed-path check that applies to every adapter.
WRITE_GUARD_SUPPORT = {
    "claude": "pre-execution",
    "hermes": "pre-execution",
    "codex": "post-run-only",
    "opencode": "post-run-only",
}


def _hermes_guard_requested(config: AgentRuntimeConfig) -> bool:
    value = config.options.get("writeGuard", False)
    if not isinstance(value, bool):
        raise DevPlaneError("Hermes runtime option writeGuard must be a boolean")
    return value


def _hermes_args(request: SessionRequest, config: AgentRuntimeConfig) -> list[str]:
    if _hermes_guard_requested(config) and request.guard_policy is None:
        raise DevPlaneError("Hermes write guard is enabled but this session has no guard policy")
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


def _claude_permission_args(config: AgentRuntimeConfig) -> list[str]:
    permission_mode = config.options.get("permissionMode", "acceptEdits")
    tools = config.options.get("allowedTools", "Read,Edit,Write,Bash")
    if not isinstance(permission_mode, str) or not isinstance(tools, str):
        raise DevPlaneError("Claude runtime options must be strings")
    return ["--permission-mode", permission_mode, "--allowedTools", tools]


_GUARDED_TOOLS = "Edit|Write|MultiEdit|NotebookEdit"
_NO_ATTRIBUTION = {"commit": "", "pr": ""}


def _claude_option_flag(config: AgentRuntimeConfig, name: str) -> bool:
    value = config.options.get(name, False)
    if not isinstance(value, bool):
        raise DevPlaneError(f"Claude runtime option {name} must be a boolean")
    return value


def _claude_write_guard_hooks(request: SessionRequest, config: AgentRuntimeConfig) -> dict[str, Any]:
    """The PreToolUse hook that runs ``devplane guard``, or nothing when disabled."""
    if not _claude_option_flag(config, "writeGuard"):
        return {}
    if request.guard_policy is None:
        raise DevPlaneError("Claude write guard is enabled but this session has no guard policy")
    command = shlex.join(
        [sys.executable, "-m", "devplane", "guard", "--policy", str(request.guard_policy)]
    )
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": _GUARDED_TOOLS,
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        }
    }


def _claude_settings_args(request: SessionRequest, config: AgentRuntimeConfig) -> list[str]:
    """One ``--settings`` argument: write guard hook and commit attribution policy.

    Claude Code adds a ``Co-Authored-By`` trailer to the commits it makes. Commits
    made by assignment agents are fast-forwarded into the project, so attribution
    is turned off unless ``commitAttribution: true`` is set.
    """
    settings: dict[str, Any] = {}
    if not _claude_option_flag(config, "commitAttribution"):
        settings["attribution"] = dict(_NO_ATTRIBUTION)
    settings.update(_claude_write_guard_hooks(request, config))
    return ["--settings", json.dumps(settings, sort_keys=True)] if settings else []


def _claude_args(request: SessionRequest, config: AgentRuntimeConfig) -> list[str]:
    args = [
        config.executable,
        "--print",
        "--output-format",
        "json",
        "--no-session-persistence",
        *_claude_permission_args(config),
        *_claude_settings_args(request, config),
    ]
    if config.model:
        args.extend(["--model", config.model])
    # --allowedTools takes any number of values, so without the separator it consumes the
    # prompt and Claude Code exits with "Input must be provided".
    args.extend(["--", request.prompt])
    return args


def _codex_sandbox_args(config: AgentRuntimeConfig) -> list[str]:
    sandbox = config.options.get("sandbox", "workspace-write")
    if not isinstance(sandbox, str) or not sandbox:
        raise DevPlaneError("Codex runtime option sandbox must be a string")
    return ["--sandbox", sandbox]


def _codex_args(request: SessionRequest, config: AgentRuntimeConfig) -> list[str]:
    args = [config.executable, "exec", *_codex_sandbox_args(config)]
    if config.model:
        args.extend(["--model", config.model])
    args.append(request.prompt)
    return args


def _opencode_agent_args(config: AgentRuntimeConfig) -> list[str]:
    agent = config.options.get("agent")
    if agent is None:
        return []
    if not isinstance(agent, str) or not agent:
        raise DevPlaneError("OpenCode runtime option agent must be a string")
    return ["--agent", agent]


def speckit_phase_args(config: AgentRuntimeConfig) -> list[str]:
    """Flags an agent needs to run a Spec Kit command without a person at the prompt."""
    if config.adapter == "claude":
        return _claude_permission_args(config)
    if config.adapter == "codex":
        return _codex_sandbox_args(config)
    if config.adapter == "opencode":
        return _opencode_agent_args(config)
    return ["--provider", config.provider] if config.provider else []


def _opencode_args(request: SessionRequest, config: AgentRuntimeConfig) -> list[str]:
    args = [config.executable, "run", "--format", "default"]
    if config.model:
        args.extend(["--model", config.model])
    args.extend(_opencode_agent_args(config))
    args.append(request.prompt)
    return args


def build_agent_args(request: SessionRequest, config: AgentRuntimeConfig) -> list[str]:
    allowed_options = {
        "hermes": {"toolsets", "maxTurns", "writeGuard"},
        "claude": {"permissionMode", "allowedTools", "writeGuard", "commitAttribution"},
        "codex": {"sandbox"},
        "opencode": {"agent"},
    }
    builders = {
        "hermes": _hermes_args,
        "claude": _claude_args,
        "codex": _codex_args,
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


def session_environment(
    request: SessionRequest, config: AgentRuntimeConfig
) -> dict[str, str] | None:
    """The environment of a session that needs the guard policy, else ``None`` (inherit).

    Hermes shell hooks are configured once, globally, and cannot be given arguments per
    session, so the policy reaches the hook through the process environment.
    """
    if config.adapter != "hermes" or request.guard_policy is None:
        return None
    if not _hermes_guard_requested(config):
        return None
    return {**os.environ, GUARD_POLICY_ENV: str(request.guard_policy)}


_HOOK_SECTION = re.compile(r"^\s*\[(?P<event>[A-Za-z_]+)\]\s*$")
_HOOK_LINE = re.compile(
    r"^\s*-\s+(?P<command>.+?)(?:\s+matcher=(?P<matcher>'[^']*'|\"[^\"]*\"))?"
    r"\s+\(timeout=\d+s,\s*(?P<status>✓ allowed|✗ not allowlisted)\)\s*$"
)
_HERMES_HOOK_HELP = (
    "install the hook in ~/.hermes/config.yaml (pre_tool_call, command "
    "'<path to devplane> guard --hermes', matcher 'write_file|patch') and approve it; "
    "see docs/runtime-adapters.md"
)


def _is_devplane_hermes_guard(command: str) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return argv[-2:] == ["guard", "--hermes"] and any("devplane" in token for token in argv[:-2])


def _matcher_covers(matcher: str | None, tool: str) -> bool:
    if not matcher:
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return matcher == tool


def verify_write_guard_ready(
    config: AgentRuntimeConfig, runner: CommandRunner | None = None
) -> None:
    """Refuse to run an unguarded Hermes session when a write guard was requested.

    Hermes reads its hooks from its own global configuration, so DevPlane cannot register
    the guard per session. It asks Hermes what is configured and approved instead, and
    fails if the guard would not run, because Hermes lets a tool call proceed when a hook
    does not run.
    """
    if config.adapter != "hermes" or not _hermes_guard_requested(config):
        return
    active_runner = runner or _default_runner()
    try:
        listing = active_runner([config.executable, "hooks", "list"], Path.cwd()).stdout or ""
    except (DevPlaneError, OSError) as exc:
        raise DevPlaneError(f"cannot check the Hermes write guard: {exc}") from exc
    event = ""
    found = False
    for line in listing.splitlines():
        section = _HOOK_SECTION.match(line)
        if section:
            event = section.group("event")
            continue
        entry = _HOOK_LINE.match(line)
        if not entry or event != "pre_tool_call" or not _is_devplane_hermes_guard(entry.group("command")):
            continue
        found = True
        if entry.group("status") != "✓ allowed":
            raise DevPlaneError(
                "the Hermes write guard hook is installed but not in Hermes' allowlist; "
                "approve it once interactively (see docs/runtime-adapters.md)"
            )
        raw = entry.group("matcher")
        matcher = ast.literal_eval(raw) if raw else None
        for tool in sorted(HERMES_WRITE_TOOLS):
            if not _matcher_covers(matcher, tool):
                raise DevPlaneError(
                    f"the Hermes write guard hook matcher {matcher!r} does not cover {tool}; "
                    "use matcher 'write_file|patch'"
                )
    if not found:
        raise DevPlaneError(f"the Hermes write guard hook is not installed: {_HERMES_HOOK_HELP}")


_USAGE_FIELDS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
    ("cache_creation_input_tokens", "cache_creation_tokens"),
)


def _split_claude_json(stdout: str) -> tuple[str, dict[str, float] | None]:
    """Split Claude Code's ``--output-format json`` into the reply text and its usage.

    Anything that is not the expected JSON object is returned untouched with no usage,
    so an unexpected output never hides what the agent said.
    """
    try:
        data = json.loads(stdout)
    except ValueError:
        return stdout, None
    if not isinstance(data, dict):
        return stdout, None
    text = data.get("result")
    reply = text if isinstance(text, str) else stdout
    raw = data.get("usage")
    usage: dict[str, float] = {}
    if isinstance(raw, dict):
        for source, target in _USAGE_FIELDS:
            value = raw.get(source)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                usage[target] = value
    cost = data.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        usage["cost_usd"] = round(float(cost), 6)
    return reply, usage or None


def _run_one(
    request: SessionRequest,
    runtime: AgentRuntimeConfig,
    runner: CommandRunner,
) -> SessionResult:
    try:
        args = build_agent_args(request, runtime)
        environment = session_environment(request, runtime)
        completed = (
            runner(args, request.cwd, environment=environment)
            if environment is not None
            else runner(args, request.cwd)
        )
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
    stdout = completed.stdout or ""
    usage = None
    if runtime.adapter == "claude":
        stdout, usage = _split_claude_json(stdout)
    return SessionResult(
        request.task_id,
        completed.returncode,
        stdout,
        None,
        completed.stderr or "",
        usage,
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
    "GUARD_POLICY_ENV",
    "WRITE_GUARD_SUPPORT",
    "AgentRuntimeConfig",
    "SessionRequest",
    "SessionResult",
    "build_agent_args",
    "list_adapters",
    "run_sessions",
    "runtime_from_manifest",
    "session_environment",
    "speckit_phase_args",
    "verify_write_guard_ready",
]
