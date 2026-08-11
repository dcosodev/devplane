from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from devplane.core import DevPlaneError
from devplane.minimax_runner import (
    SessionRequest,
    SessionResult,
    build_hermes_args,
    run_sessions,
)


def test_session_request_and_result_are_dataclasses() -> None:
    request = SessionRequest(task_id="t1", prompt="hello", cwd=Path("/tmp"))
    result = SessionResult(task_id="t1", returncode=0, stdout="out", error=None, stderr="err")
    assert request.task_id == "t1"
    assert request.prompt == "hello"
    assert request.cwd == Path("/tmp")
    assert result.returncode == 0
    assert result.error is None
    assert result.stderr == "err"


def test_build_hermes_args_pins_provider_model_toolsets_quiet() -> None:
    request = SessionRequest(task_id="t1", prompt="do thing", cwd=Path("/tmp"))
    args = build_hermes_args(request)
    assert args[:4] == ["hermes", "chat", "--provider", "minimax-oauth"]
    assert "--model" in args
    model_index = args.index("--model")
    assert args[model_index + 1] == "MiniMax-M3"
    assert "--toolsets" in args
    toolsets_index = args.index("--toolsets")
    assert args[toolsets_index + 1] == "terminal,file"
    assert "--quiet" in args
    assert args[args.index("--max-turns") + 1] == "80"
    assert args[-2] == "--query"
    # prompt passed as a single argv entry, not via shell
    assert args[-1] == "do thing"
    # Fixed flag block: hermes, chat, then 4 flag/value pairs, then --quiet,
    # then the prompt as a single argv entry.
    expected_prefix = [
        "hermes",
        "chat",
        "--provider", "minimax-oauth",
        "--model", "MiniMax-M3",
        "--toolsets", "terminal,file",
        "--quiet",
        "--max-turns", "80",
        "--query",
    ]
    assert args[:-1] == expected_prefix


def test_build_hermes_args_never_uses_shell() -> None:
    request = SessionRequest(task_id="t1", prompt="echo hi; rm -rf /", cwd=Path("/tmp"))
    args = build_hermes_args(request)
    # No shell metacharacters should ever be split across argv positions.
    assert "echo" not in args
    assert "hi;" not in args
    assert "rm" not in args
    # Whole prompt is one argv token.
    assert args[-1] == "echo hi; rm -rf /"


def test_run_sessions_rejects_invalid_max_workers() -> None:
    requests = [SessionRequest(task_id="t1", prompt="p", cwd=Path("/tmp"))]
    with pytest.raises(DevPlaneError):
        run_sessions(requests, max_workers=0)
    with pytest.raises(DevPlaneError):
        run_sessions(requests, max_workers=9)
    with pytest.raises(DevPlaneError):
        run_sessions(requests, max_workers=-1)


def test_run_sessions_preserves_input_order() -> None:
    def fake_runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        # Force out-of-order completion by sleeping inversely with prompt length.
        delay = float(args[-1].count("a"))
        if delay:
            import time

            time.sleep(delay / 1000.0)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=args[-1], stderr="")

    requests = [
        SessionRequest(task_id="first", prompt="a", cwd=Path("/tmp")),
        SessionRequest(task_id="second", prompt="aa", cwd=Path("/tmp")),
        SessionRequest(task_id="third", prompt="aaa", cwd=Path("/tmp")),
    ]
    results = run_sessions(requests, max_workers=3, runner=fake_runner)
    assert [r.task_id for r in results] == ["first", "second", "third"]


def test_run_sessions_uses_thread_pool_with_capped_workers() -> None:
    seen_max: list[int] = []
    lock = threading.Lock()

    def fake_runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        with lock:
            seen_max.append(threading.active_count())
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    requests = [
        SessionRequest(task_id=f"t{i}", prompt="p", cwd=Path("/tmp"))
        for i in range(6)
    ]
    run_sessions(requests, max_workers=4, runner=fake_runner)
    # max_workers=4 should not exceed 4 concurrent worker threads plus main.
    assert max(seen_max) <= 5  # 4 workers + main


def test_run_sessions_collects_stdout_per_task() -> None:
    def fake_runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=f"output-for-{args[-1]}", stderr=""
        )

    requests = [
        SessionRequest(task_id="a", prompt="alpha", cwd=Path("/tmp")),
        SessionRequest(task_id="b", prompt="beta", cwd=Path("/tmp")),
    ]
    results = run_sessions(requests, max_workers=2, runner=fake_runner)
    by_id = {r.task_id: r for r in results}
    assert by_id["a"].returncode == 0
    assert by_id["a"].stdout == "output-for-alpha"
    assert by_id["b"].stdout == "output-for-beta"


def test_run_sessions_converts_devplane_error_to_failed_result() -> None:
    def failing_runner(args: list[str], cwd: Path) -> Any:
        raise DevPlaneError("required executable not found: hermes")

    requests = [
        SessionRequest(task_id="boom", prompt="p", cwd=Path("/tmp")),
        SessionRequest(task_id="ok", prompt="q", cwd=Path("/tmp")),
    ]

    def ok_runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

    def selective(
        runners: dict[str, Any],
        call_log: list[str],
        lock: threading.Lock,
    ):
        def runner(args: list[str], cwd: Path) -> Any:
            with lock:
                call_log.append(args[-1])
            if args[-1] == "p":
                raise DevPlaneError("boom")
            return ok_runner(args, cwd)

        return runner

    lock = threading.Lock()
    log: list[str] = []
    runner = selective({}, log, lock)
    results = run_sessions(requests, max_workers=2, runner=runner)
    by_id = {r.task_id: r for r in results}
    assert by_id["boom"].returncode != 0
    assert "boom" in (by_id["boom"].error or "")
    assert by_id["ok"].returncode == 0
    assert by_id["ok"].stdout == "ok"


def test_run_sessions_default_runner_is_run_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_checked(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["cwd"] = cwd
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="x", stderr="")

    from devplane import commands

    monkeypatch.setattr(commands, "run_checked", fake_run_checked)
    cwd = Path("/tmp/work")
    requests = [SessionRequest(task_id="t1", prompt="do it", cwd=cwd)]
    results = run_sessions(requests, max_workers=1)
    assert captured["args"][-1] == "do it"
    assert captured["cwd"] == cwd
    assert results[0].returncode == 0
    assert results[0].stdout == "x"


def test_run_sessions_isolates_unexpected_runner_exception(tmp_path: Path) -> None:
    secret = "private-prompt-value"

    def broken_runner(args, cwd):
        raise RuntimeError(secret)

    results = run_sessions(
        [SessionRequest("t1", "prompt", tmp_path)],
        max_workers=1,
        runner=broken_runner,
    )

    assert results[0].returncode == 1
    assert results[0].error == "unexpected runner error: RuntimeError"
    assert secret not in results[0].error
