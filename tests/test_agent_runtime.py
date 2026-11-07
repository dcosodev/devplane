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
        provider=None,
        model=None,
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
    assert list_adapters() == ["claude", "codex", "hermes", "opencode"]


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
    # --allowedTools takes many values: only the separator keeps the prompt out of them.
    assert args[-2:] == ["--", "implement task"]
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


def test_codex_adapter_builds_shell_free_exec_argv() -> None:
    request = SessionRequest("T1", "Implement T1", Path("/repo"))
    config = AgentRuntimeConfig("codex", "codex", None, "gpt-test", {})

    assert build_agent_args(request, config) == [
        "codex", "exec", "--sandbox", "workspace-write", "--model", "gpt-test", "Implement T1",
    ]


def test_codex_adapter_rejects_provider() -> None:
    request = SessionRequest("T1", "Implement T1", Path("/repo"))
    config = AgentRuntimeConfig("codex", "codex", "some-provider", None, {})

    with pytest.raises(DevPlaneError, match="does not accept provider"):
        build_agent_args(request, config)


def _guarded_config(**options: object) -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        adapter="claude",
        executable="claude",
        provider=None,
        model=None,
        options={"writeGuard": True, **options},  # type: ignore[dict-item]
    )


def test_claude_adapter_registers_the_write_guard_hook_when_enabled() -> None:
    import json
    import shlex
    import sys

    policy = Path("/state/guard-a1.json")
    request = SessionRequest("a1", "implement task", Path("/tmp/work"), guard_policy=policy)

    args = build_agent_args(request, _guarded_config())

    settings = json.loads(args[args.index("--settings") + 1])
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    assert shlex.split(hook["command"]) == [
        sys.executable, "-m", "devplane", "guard", "--policy", str(policy),
    ]
    assert args[-1] == request.prompt


def _claude_settings(args: list[str]) -> dict:
    import json

    return json.loads(args[args.index("--settings") + 1])


def test_claude_adapter_turns_off_commit_attribution_by_default() -> None:
    request = SessionRequest("a1", "implement task", Path("/tmp/work"))
    config = AgentRuntimeConfig("claude", "claude", None, None, {})

    args = build_agent_args(request, config)

    assert _claude_settings(args) == {"attribution": {"commit": "", "pr": ""}}
    assert args[-2:] == ["--", "implement task"]


def test_claude_adapter_keeps_attribution_when_commit_attribution_is_enabled() -> None:
    request = SessionRequest("a1", "implement task", Path("/tmp/work"))
    config = AgentRuntimeConfig("claude", "claude", None, None, {"commitAttribution": True})

    assert "--settings" not in build_agent_args(request, config)


def test_the_write_guard_and_attribution_settings_share_one_settings_argument() -> None:
    policy = Path("/state/guard-a1.json")
    request = SessionRequest("a1", "x", Path("/tmp/work"), guard_policy=policy)

    both = build_agent_args(request, _guarded_config())
    guard_only = build_agent_args(request, _guarded_config(commitAttribution=True))

    assert both.count("--settings") == guard_only.count("--settings") == 1
    assert set(_claude_settings(both)) == {"attribution", "hooks"}
    assert set(_claude_settings(guard_only)) == {"hooks"}


def test_commit_attribution_option_must_be_a_boolean() -> None:
    request = SessionRequest("a1", "x", Path("/tmp/work"))
    config = AgentRuntimeConfig("claude", "claude", None, None, {"commitAttribution": "yes"})

    with pytest.raises(DevPlaneError, match="commitAttribution"):
        build_agent_args(request, config)


@pytest.mark.parametrize("adapter", ["hermes", "codex", "opencode"])
def test_other_adapters_reject_the_commit_attribution_option(adapter: str) -> None:
    request = SessionRequest("a1", "x", Path("/tmp/work"))
    config = AgentRuntimeConfig(adapter, adapter, None, None, {"commitAttribution": True})

    with pytest.raises(DevPlaneError, match="unsupported"):
        build_agent_args(request, config)


def test_enabled_write_guard_without_a_policy_fails_closed() -> None:
    request = SessionRequest("a1", "implement task", Path("/tmp/work"))

    with pytest.raises(DevPlaneError, match="write guard"):
        build_agent_args(request, _guarded_config())


def test_write_guard_option_must_be_a_boolean() -> None:
    request = SessionRequest("a1", "x", Path("/tmp/work"), guard_policy=Path("/state/g.json"))

    with pytest.raises(DevPlaneError, match="writeGuard"):
        build_agent_args(
            request, AgentRuntimeConfig("claude", "claude", None, None, {"writeGuard": "yes"})
        )


@pytest.mark.parametrize("adapter", ["codex", "opencode"])
def test_adapters_without_a_pre_execution_hook_reject_the_write_guard_option(adapter: str) -> None:
    request = SessionRequest("a1", "x", Path("/tmp/work"))
    config = AgentRuntimeConfig(adapter, adapter, None, None, {"writeGuard": True})

    with pytest.raises(DevPlaneError, match="unsupported"):
        build_agent_args(request, config)


# ---------------------------------------------------------------------------
# Hermes write guard
# ---------------------------------------------------------------------------

import os
import subprocess as _subprocess

from devplane.agent_runtime import (
    WRITE_GUARD_SUPPORT,
    session_environment,
    verify_write_guard_ready,
)


def _hermes_config(**options: object) -> AgentRuntimeConfig:
    return AgentRuntimeConfig("hermes", "hermes", None, None, dict(options))  # type: ignore[arg-type]


def test_hermes_accepts_the_write_guard_option_and_needs_a_policy_for_it() -> None:
    config = _hermes_config(writeGuard=True)

    with pytest.raises(DevPlaneError, match="write guard"):
        build_agent_args(SessionRequest("t", "x", Path("/tmp/w")), config)

    args = build_agent_args(SessionRequest("t", "x", Path("/tmp/w"), guard_policy=Path("/s/g.json")), config)
    assert args[:2] == ["hermes", "chat"]
    assert "--settings" not in args


def test_hermes_write_guard_option_must_be_a_boolean() -> None:
    request = SessionRequest("t", "x", Path("/tmp/w"), guard_policy=Path("/s/g.json"))

    with pytest.raises(DevPlaneError, match="writeGuard"):
        build_agent_args(request, _hermes_config(writeGuard="yes"))


def test_session_environment_exports_the_policy_only_for_a_guarded_hermes_session() -> None:
    policy = Path("/state/guard-t.json")
    guarded = SessionRequest("t", "x", Path("/tmp/w"), guard_policy=policy)

    environment = session_environment(guarded, _hermes_config(writeGuard=True))

    assert environment is not None
    assert environment["DEVPLANE_GUARD_POLICY"] == str(policy)
    assert environment["PATH"] == os.environ["PATH"]
    assert session_environment(guarded, _hermes_config()) is None
    assert session_environment(SessionRequest("t", "x", Path("/tmp/w")), _hermes_config(writeGuard=True)) is None
    claude = AgentRuntimeConfig("claude", "claude", None, None, {"writeGuard": True})
    assert session_environment(guarded, claude) is None


def test_run_sessions_hands_the_policy_environment_to_the_runner() -> None:
    seen: list[dict | None] = []

    def runner(args: list[str], cwd: Path, **kwargs: object) -> _subprocess.CompletedProcess[str]:
        seen.append(kwargs.get("environment"))  # type: ignore[arg-type]
        return _subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    guarded = SessionRequest("t1", "x", Path("/tmp/w"), guard_policy=Path("/state/g1.json"))
    plain = SessionRequest("t2", "x", Path("/tmp/w"))

    run_sessions([guarded], 1, runtime=_hermes_config(writeGuard=True), runner=runner)
    run_sessions([plain], 1, runtime=_hermes_config(), runner=runner)

    assert seen[0] is not None and seen[0]["DEVPLANE_GUARD_POLICY"] == "/state/g1.json"
    assert seen[1] is None


def test_adapters_declare_their_write_guard_support() -> None:
    assert WRITE_GUARD_SUPPORT == {
        "claude": "pre-execution",
        "hermes": "pre-execution",
        "codex": "post-run-only",
        "opencode": "post-run-only",
    }


LIST_HEADER = "Configured shell hooks ({count} total):\n\n"


def _hooks_list(*entries: tuple[str, str, str | None, bool]) -> str:
    """Render ``hermes hooks list`` output: (event, command, matcher, allowed)."""
    lines = [LIST_HEADER.format(count=len(entries))]
    for event in sorted({entry[0] for entry in entries}):
        lines.append(f"  [{event}]\n")
        for entry_event, command, matcher, allowed in entries:
            if entry_event != event:
                continue
            matcher_part = f" matcher={matcher!r}" if matcher else ""
            status = "✓ allowed" if allowed else "✗ not allowlisted"
            lines.append(f"    - {command}{matcher_part} (timeout=10s, {status})\n")
            if allowed:
                lines.append("      approved_at: 2026-11-03T10:00:00+00:00\n")
        lines.append("\n")
    return "".join(lines)


def _list_runner(output: str, calls: list | None = None):
    def runner(args: list[str], cwd: Path, **kwargs: object) -> _subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(args)
        return _subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    return runner


GUARD_COMMAND = "/opt/venv/bin/devplane guard --hermes"


@pytest.mark.parametrize("matcher", ["write_file|patch", "(write_file|patch)", None, ".*"])
def test_verify_accepts_an_installed_and_approved_hook_that_covers_both_tools(matcher: str | None) -> None:
    calls: list = []
    output = _hooks_list(("pre_tool_call", GUARD_COMMAND, matcher, True))

    verify_write_guard_ready(_hermes_config(writeGuard=True), _list_runner(output, calls))

    assert calls == [["hermes", "hooks", "list"]]


def test_verify_accepts_the_hook_next_to_unrelated_hooks() -> None:
    output = _hooks_list(
        ("post_tool_call", "/bin/audit-log", None, True),
        ("pre_tool_call", "/bin/other-hook --flag", "terminal", True),
        ("pre_tool_call", GUARD_COMMAND, "write_file|patch", True),
    )

    verify_write_guard_ready(_hermes_config(writeGuard=True), _list_runner(output))


@pytest.mark.parametrize(
    ("output", "problem"),
    [
        ("No shell hooks configured in ~/.hermes/config.yaml.\n", "not installed"),
        (_hooks_list(("pre_tool_call", "/bin/other-hook", None, True)), "not installed"),
        (_hooks_list(("post_tool_call", GUARD_COMMAND, None, True)), "not installed"),
        (_hooks_list(("pre_tool_call", GUARD_COMMAND, "write_file|patch", False)), "allowlist"),
        (_hooks_list(("pre_tool_call", GUARD_COMMAND, "write_file", True)), "patch"),
        (_hooks_list(("pre_tool_call", GUARD_COMMAND, "terminal", True)), "write_file"),
        (_hooks_list(("pre_tool_call", "/bin/devplane guard", None, True)), "not installed"),
        ("something completely different\n", "not installed"),
        ("", "not installed"),
    ],
    ids=["none", "other-hook", "wrong-event", "not-allowlisted", "misses-patch", "misses-write", "no-hermes-flag", "garbage", "empty"],
)
def test_verify_refuses_when_the_hook_is_not_effective(output: str, problem: str) -> None:
    with pytest.raises(DevPlaneError, match=problem):
        verify_write_guard_ready(_hermes_config(writeGuard=True), _list_runner(output))


def test_verify_points_to_the_documentation_and_the_exact_hook_command() -> None:
    with pytest.raises(DevPlaneError, match=r"guard --hermes.*runtime-adapters\.md"):
        verify_write_guard_ready(_hermes_config(writeGuard=True), _list_runner(""))


def test_verify_refuses_when_hermes_cannot_be_queried() -> None:
    def broken(args: list[str], cwd: Path, **kwargs: object):
        raise DevPlaneError("required executable not found: hermes")

    with pytest.raises(DevPlaneError, match="hermes"):
        verify_write_guard_ready(_hermes_config(writeGuard=True), broken)


def test_verify_does_nothing_when_no_hermes_guard_was_requested() -> None:
    calls: list = []
    runner = _list_runner("", calls)

    verify_write_guard_ready(_hermes_config(), runner)
    verify_write_guard_ready(_hermes_config(writeGuard=False), runner)
    verify_write_guard_ready(AgentRuntimeConfig("claude", "claude", None, None, {"writeGuard": True}), runner)
    verify_write_guard_ready(AgentRuntimeConfig("codex", "codex", None, None, {}), runner)

    assert calls == []


# ---------------------------------------------------------------------------
# token and cost capture (Claude Code json output)
# ---------------------------------------------------------------------------

import json as _json

from devplane.agent_runtime import _split_claude_json
from devplane.pipeline import AssignmentRun, total_usage


def test_claude_sessions_ask_for_json_output() -> None:
    request = SessionRequest("t1", "implement task", Path("/tmp/work"))
    config = AgentRuntimeConfig("claude", "claude", None, None, {})

    args = build_agent_args(request, config)

    assert args[args.index("--output-format") + 1] == "json"


def test_claude_json_is_split_into_reply_and_usage() -> None:
    payload = _json.dumps(
        {
            "result": "done",
            "total_cost_usd": 0.1234567,
            "usage": {
                "input_tokens": 3,
                "output_tokens": 40,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 100,
                "service_tier": "standard",
            },
        }
    )

    text, usage = _split_claude_json(payload)

    assert text == "done"
    assert usage == {
        "input_tokens": 3,
        "output_tokens": 40,
        "cache_read_tokens": 900,
        "cache_creation_tokens": 100,
        "cost_usd": 0.123457,
    }


@pytest.mark.parametrize("stdout", ["plain text", "[1, 2]", "", '{"usage": {"input_tokens": true}}'])
def test_unexpected_claude_output_is_kept_and_reports_no_usage(stdout: str) -> None:
    text, usage = _split_claude_json(stdout)

    assert text == stdout
    assert usage is None


def test_run_sessions_attaches_usage_for_claude_only() -> None:
    def runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        body = _json.dumps({"result": "ok", "usage": {"output_tokens": 7}})
        return subprocess.CompletedProcess(args, 0, stdout=body, stderr="")

    request = [SessionRequest("one", "go", Path("/tmp/one"))]
    claude = run_sessions(
        request, 1, runtime=AgentRuntimeConfig("claude", "claude", None, None, {}), runner=runner
    )
    codex = run_sessions(
        request, 1, runtime=AgentRuntimeConfig("codex", "codex", None, None, {}), runner=runner
    )

    assert (claude[0].stdout, claude[0].usage) == ("ok", {"output_tokens": 7})
    assert codex[0].usage is None and codex[0].stdout.startswith("{")


def test_total_usage_adds_only_what_was_reported() -> None:
    runs = (
        AssignmentRun("a", "A", "completed", usage={"output_tokens": 5, "cost_usd": 0.1}),
        AssignmentRun("b", "B", "completed"),
        AssignmentRun("c", "C", "failed", usage={"output_tokens": 2, "cost_usd": 0.2}),
    )

    assert total_usage(runs) == {"output_tokens": 7, "cost_usd": 0.3}
    assert total_usage(runs[1:2]) is None
