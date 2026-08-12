from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devplane.agent_runtime import (
    AgentRuntimeConfig,
    SessionRequest,
    build_agent_args,
    list_adapters,
    run_sessions,
    runtime_from_manifest,
)
from devplane.core import DevPlaneError


def test_runtime_from_manifest_normalizes_legacy_hermes_configuration() -> None:
    config = runtime_from_manifest({"spec": {"runtime": {"agent": "hermes"}}})

    assert config == AgentRuntimeConfig(
        adapter="hermes",
        executable="hermes",
        provider="minimax-oauth",
        model="MiniMax-M3",
        options={},
    )


def test_runtime_from_manifest_accepts_neutral_adapter_configuration() -> None:
    config = runtime_from_manifest(
        {
            "spec": {
                "runtime": {
                    "adapter": "opencode",
                    "model": "openai/gpt-5.3-codex",
                    "options": {"agent": "build"},
                }
            }
        }
    )

    assert config.adapter == "opencode"
    assert config.executable == "opencode"
    assert config.model == "openai/gpt-5.3-codex"
    assert config.options == {"agent": "build"}


def test_runtime_from_manifest_requires_configured_runtime() -> None:
    with pytest.raises(DevPlaneError, match="no agent runtime"):
        runtime_from_manifest({"spec": {}})


def test_runtime_from_manifest_rejects_unsupported_adapter() -> None:
    with pytest.raises(DevPlaneError, match="unsupported agent adapter: unknown"):
        runtime_from_manifest(
            {"spec": {"runtime": {"adapter": "unknown", "executable": "unknown"}}}
        )


def test_builtin_adapters_are_public_and_agent_neutral() -> None:
    assert list_adapters() == ["claude", "hermes", "opencode"]


def test_hermes_adapter_builds_configurable_shell_free_argv() -> None:
    request = SessionRequest("t1", "edit safely; never split me", Path("/tmp/work"))
    config = AgentRuntimeConfig(
        adapter="hermes",
        executable="custom-hermes",
        provider="openai-codex",
        model="gpt-5.3-codex",
        options={"toolsets": "terminal,file", "maxTurns": 40},
    )

    args = build_agent_args(request, config)

    assert args[:4] == ["custom-hermes", "chat", "--provider", "openai-codex"]
    assert args[args.index("--model") + 1] == "gpt-5.3-codex"
    assert args[args.index("--max-turns") + 1] == "40"
    assert args[-2:] == ["--query", request.prompt]
    assert "edit" not in args


def test_claude_adapter_is_noninteractive_without_bypassing_permissions() -> None:
    request = SessionRequest("t1", "implement task", Path("/tmp/work"))
    config = AgentRuntimeConfig(
        adapter="claude",
        executable="claude",
        provider=None,
        model="sonnet",
        options={},
    )

    args = build_agent_args(request, config)

    assert args[:2] == ["claude", "--print"]
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in args
    assert args[-1] == request.prompt


def test_opencode_adapter_uses_verified_headless_run_command() -> None:
    request = SessionRequest("t1", "implement task", Path("/tmp/work"))
    config = AgentRuntimeConfig(
        adapter="opencode",
        executable="opencode",
        provider=None,
        model="anthropic/claude-sonnet-4-5",
        options={"agent": "build"},
    )

    args = build_agent_args(request, config)

    assert args[:3] == ["opencode", "run", "--format"]
    assert args[args.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
    assert args[args.index("--agent") + 1] == "build"
    assert "--dangerously-skip-permissions" not in args
    assert args[-1] == request.prompt


def test_run_sessions_dispatches_with_selected_adapter_and_preserves_order() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=args[-1], stderr="")

    requests = [
        SessionRequest("one", "first", Path("/tmp/one")),
        SessionRequest("two", "second", Path("/tmp/two")),
    ]
    config = AgentRuntimeConfig("claude", "claude", None, None, {})

    results = run_sessions(requests, max_workers=2, runtime=config, runner=runner)

    assert [result.task_id for result in results] == ["one", "two"]
    assert all(args[0] == "claude" for args in calls)


def test_unknown_adapter_is_rejected_before_execution() -> None:
    config = AgentRuntimeConfig("unknown", "unknown", None, None, {})
    request = SessionRequest("t1", "task", Path("/tmp"))

    with pytest.raises(DevPlaneError, match="unsupported agent adapter"):
        build_agent_args(request, config)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            AgentRuntimeConfig("hermes", "hermes", None, None, {"mystery": True}),
            "unsupported hermes runtime options",
        ),
        (
            AgentRuntimeConfig("claude", "claude", "anthropic", None, {}),
            "claude adapter does not accept provider",
        ),
        (
            AgentRuntimeConfig("opencode", "opencode", None, None, {"plugin": "x"}),
            "unsupported opencode runtime options",
        ),
    ],
)
def test_adapter_specific_configuration_fails_closed(
    config: AgentRuntimeConfig, message: str
) -> None:
    request = SessionRequest("T001", "Do work", Path("/tmp/work"))

    with pytest.raises(DevPlaneError, match=message):
        build_agent_args(request, config)
