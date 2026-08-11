from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devplane.commands import run_checked
from devplane.core import DevPlaneError


def test_command_failure_does_not_echo_sensitive_arguments(monkeypatch, tmp_path: Path) -> None:
    secret = "private-feature-description"

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(9, ["specify", "--input", f"spec={secret}"])

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(DevPlaneError) as error:
        run_checked(["specify", "--input", f"spec={secret}"], tmp_path)
    assert secret not in str(error.value)
    assert "specify" in str(error.value)


def test_run_checked_captures_stderr_and_applies_timeout(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    def succeed(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="session_id: abc")

    monkeypatch.setattr(subprocess, "run", succeed)
    result = run_checked(["hermes", "chat"], tmp_path)

    assert result.stderr == "session_id: abc"
    assert captured["stderr"] is subprocess.PIPE
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["timeout"] == 900
    assert captured["shell"] is False


def test_run_checked_reports_timeout_without_echoing_arguments(monkeypatch, tmp_path: Path) -> None:
    secret = "private-feature-description"

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["hermes", secret], 900)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(DevPlaneError) as error:
        run_checked(["hermes", secret], tmp_path)

    assert "timed out" in str(error.value)
    assert secret not in str(error.value)
