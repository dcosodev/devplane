from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from devplane.agent_runtime import (
    AgentRuntimeConfig,
    SessionRequest,
    SessionResult,
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


def test_session_request_and_result_expose_the_neutral_fields() -> None:
    request = SessionRequest(task_id="t1", prompt="hello", cwd=Path("/tmp"))
    result = SessionResult(task_id="t1", returncode=0, stdout="out", error=None, stderr="err")

    assert (request.task_id, request.prompt, request.cwd) == ("t1", "hello", Path("/tmp"))
    assert (result.returncode, result.stdout, result.error, result.stderr) == (0, "out", None, "err")


def test_hermes_adapter_applies_default_options_when_none_are_configured() -> None:
    request = SessionRequest("t1", "do thing", Path("/tmp"))
    config = AgentRuntimeConfig("hermes", "hermes", None, None, {})

    assert build_agent_args(request, config) == [
        "hermes",
        "chat",
        "--toolsets",
        "terminal,file",
        "--quiet",
        "--max-turns",
        "80",
        "--source",
        "tool",
        "--query",
        "do thing",
    ]


@pytest.mark.parametrize(
    "config",
    [
        AgentRuntimeConfig("hermes", "hermes", "any-provider", "any-model", {}),
        AgentRuntimeConfig("claude", "claude", None, "sonnet", {}),
        AgentRuntimeConfig("opencode", "opencode", None, "any/model", {}),
    ],
    ids=lambda config: config.adapter,
)
def test_every_adapter_passes_a_hostile_prompt_as_one_argv_token(
    config: AgentRuntimeConfig,
) -> None:
    prompt = "echo hi; rm -rf /"

    args = build_agent_args(SessionRequest("t1", prompt, Path("/tmp")), config)

    assert args[-1] == prompt
    assert args.count(prompt) == 1
    assert not {"echo", "hi;", "rm", "-rf"} & set(args)


@pytest.mark.parametrize("max_workers", [0, 9, -1, True, "2"])
def test_run_sessions_rejects_invalid_max_workers(max_workers: Any) -> None:
    config = AgentRuntimeConfig("claude", "claude", None, None, {})
    requests = [SessionRequest("t1", "p", Path("/tmp"))]

    with pytest.raises(DevPlaneError, match="max_workers"):
        run_sessions(requests, max_workers=max_workers, runtime=config)


def test_run_sessions_preserves_input_order_when_completion_order_differs() -> None:
    second_finished = threading.Event()
    completion: list[str] = []

    def runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args[-1] == "first":
            assert second_finished.wait(timeout=10)
        completion.append(args[-1])
        if args[-1] == "second":
            second_finished.set()
        return subprocess.CompletedProcess(args, 0, stdout=args[-1], stderr="")

    config = AgentRuntimeConfig("claude", "claude", None, None, {})
    results = run_sessions(
        [
            SessionRequest("one", "first", Path("/tmp")),
            SessionRequest("two", "second", Path("/tmp")),
        ],
        max_workers=2,
        runtime=config,
        runner=runner,
    )

    assert completion == ["second", "first"]
    assert [result.task_id for result in results] == ["one", "two"]


def test_run_sessions_runs_concurrently_up_to_the_worker_cap() -> None:
    # The barrier only releases with two sessions in flight, proving concurrency.
    barrier = threading.Barrier(2, timeout=10)
    lock = threading.Lock()
    active = 0
    peak = 0

    def runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait()
        with lock:
            active -= 1
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    config = AgentRuntimeConfig("claude", "claude", None, None, {})
    requests = [SessionRequest(f"t{index}", "p", Path("/tmp")) for index in range(6)]

    results = run_sessions(requests, max_workers=2, runtime=config, runner=runner)

    assert [result.returncode for result in results] == [0] * 6
    assert peak == 2


def test_run_sessions_collects_output_per_task() -> None:
    def runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 0, stdout=f"out-{args[-1]}", stderr=f"err-{args[-1]}"
        )

    config = AgentRuntimeConfig("claude", "claude", None, None, {})
    results = run_sessions(
        [
            SessionRequest("a", "alpha", Path("/tmp")),
            SessionRequest("b", "beta", Path("/tmp")),
        ],
        max_workers=2,
        runtime=config,
        runner=runner,
    )

    by_id = {result.task_id: result for result in results}
    assert (by_id["a"].returncode, by_id["a"].stdout, by_id["a"].stderr) == (
        0,
        "out-alpha",
        "err-alpha",
    )
    assert (by_id["b"].stdout, by_id["b"].stderr) == ("out-beta", "err-beta")


def test_run_sessions_converts_devplane_error_to_one_failed_result() -> None:
    def runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args[-1] == "bad":
            raise DevPlaneError("boom")
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    config = AgentRuntimeConfig("claude", "claude", None, None, {})
    results = run_sessions(
        [
            SessionRequest("fails", "bad", Path("/tmp")),
            SessionRequest("passes", "good", Path("/tmp")),
        ],
        max_workers=2,
        runtime=config,
        runner=runner,
    )

    by_id = {result.task_id: result for result in results}
    assert by_id["fails"].returncode != 0
    assert by_id["fails"].error == "boom"
    assert by_id["passes"].returncode == 0
    assert by_id["passes"].stdout == "ok"


def test_run_sessions_default_runner_is_run_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_checked(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        captured.update(args=args, cwd=cwd)
        return subprocess.CompletedProcess(args, 0, stdout="x", stderr="")

    from devplane import commands

    monkeypatch.setattr(commands, "run_checked", fake_run_checked)
    cwd = Path("/tmp/work")
    config = AgentRuntimeConfig("claude", "claude", None, None, {})

    results = run_sessions(
        [SessionRequest("t1", "do it", cwd)], max_workers=1, runtime=config
    )

    assert captured["args"][-1] == "do it"
    assert captured["cwd"] == cwd
    assert (results[0].returncode, results[0].stdout) == (0, "x")


def test_run_sessions_isolates_unexpected_runner_exception() -> None:
    secret = "private-prompt-value"

    def runner(args: list[str], cwd: Path) -> Any:
        raise RuntimeError(secret)

    config = AgentRuntimeConfig("claude", "claude", None, None, {})
    results = run_sessions(
        [SessionRequest("t1", "prompt", Path("/tmp"))],
        max_workers=1,
        runtime=config,
        runner=runner,
    )

    assert results[0].returncode == 1
    assert results[0].error == "unexpected runner error: RuntimeError"
    assert secret not in results[0].error
