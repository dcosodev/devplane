from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devplane.commands import describe_dirty, run_checked
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


def test_describe_dirty_names_the_first_paths_with_their_status_codes() -> None:
    status = "?? calc/__pycache__/\n M src/a.py\nA  new.txt\n"

    assert describe_dirty(status) == ": ?? calc/__pycache__/, M src/a.py, A new.txt"


def test_describe_dirty_summarises_long_listings() -> None:
    status = "".join(f"?? file{index}.txt\n" for index in range(8))

    described = describe_dirty(status)

    assert described.startswith(": ?? file0.txt, ?? file1.txt")
    assert "file4.txt" in described and "file5.txt" not in described
    assert described.endswith("(and 3 more)")


def test_describe_dirty_is_empty_for_a_clean_status() -> None:
    assert describe_dirty("") == ""
    assert describe_dirty("\n  \n") == ""
