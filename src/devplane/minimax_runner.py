"""Bounded parallel runner for Hermes MiniMax sessions.

Spawns shell-free `hermes chat` invocations through a thread pool, preserving
input order and converting deterministic DevPlane failures into per-task
SessionResult records instead of raising.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .core import DevPlaneError

# Constants pinned by the contract; kept module-level so tests can assert
# exact argv construction without duplicating literals.
HERMES_PROVIDER = "minimax-oauth"
HERMES_MODEL = "MiniMax-M3"
HERMES_TOOLSETS = "terminal,file"
MIN_WORKERS = 1
MAX_WORKERS = 8


class _Runner(Protocol):
    def __call__(
        self, args: list[str], cwd: Path
    ) -> subprocess.CompletedProcess[str]: ...


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


def build_hermes_args(request: SessionRequest) -> list[str]:
    """Build the argv for a Hermes MiniMax session.

    The prompt is passed as a single argv token so the runner never invokes a
    shell. The fixed flags pin the runtime contract expected by DevPlane.
    """
    return [
        "hermes",
        "chat",
        "--provider", HERMES_PROVIDER,
        "--model", HERMES_MODEL,
        "--toolsets", HERMES_TOOLSETS,
        "--quiet",
        "--max-turns", "80",
        "--query",
        request.prompt,
    ]


def _validate_workers(max_workers: int) -> None:
    if not isinstance(max_workers, int) or isinstance(max_workers, bool):
        raise DevPlaneError(f"max_workers must be an integer: {max_workers!r}")
    if max_workers < MIN_WORKERS or max_workers > MAX_WORKERS:
        raise DevPlaneError(
            f"max_workers must be between {MIN_WORKERS} and {MAX_WORKERS}: {max_workers}"
        )


def _resolve_runner(runner: _Runner | None) -> _Runner:
    if runner is None:
        # Late binding so monkeypatching devplane.commands.run_checked is honored.
        from .commands import run_checked

        return run_checked
    return runner


def _run_one(
    request: SessionRequest,
    runner: _Runner,
) -> SessionResult:
    args = build_hermes_args(request)
    try:
        completed = runner(args, request.cwd)
    except DevPlaneError as exc:
        return SessionResult(
            task_id=request.task_id,
            returncode=1,
            stdout="",
            error=str(exc),
            stderr="",
        )
    except FileNotFoundError as exc:
        # Defensive: a raw FileNotFoundError should still surface as a failed
        # result, not crash the whole pool.
        return SessionResult(
            task_id=request.task_id,
            returncode=1,
            stdout="",
            error=f"required executable not found: {exc}",
            stderr="",
        )
    except Exception as exc:  # noqa: BLE001 - isolate unexpected worker failures
        return SessionResult(
            task_id=request.task_id,
            returncode=1,
            stdout="",
            error=f"unexpected runner error: {type(exc).__name__}",
            stderr="",
        )
    return SessionResult(
        task_id=request.task_id,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        error=None,
        stderr=completed.stderr or "",
    )


def run_sessions(
    requests: list[SessionRequest],
    max_workers: int,
    runner: _Runner | None = None,
) -> list[SessionResult]:
    """Run Hermes sessions in parallel and return results in input order."""
    _validate_workers(max_workers)
    active_runner = _resolve_runner(runner)

    if not requests:
        return []

    # Worker count is capped at the request count so we never spawn idle
    # threads; the upper bound is already enforced by _validate_workers.
    workers = min(max_workers, len(requests))

    results: list[SessionResult | None] = [None] * len(requests)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_one, request, active_runner): index
            for index, request in enumerate(requests)
        }
        for future, index in futures.items():
            results[index] = future.result()

    return [result for result in results if result is not None]


__all__ = [
    "SessionRequest",
    "SessionResult",
    "build_hermes_args",
    "run_sessions",
]
