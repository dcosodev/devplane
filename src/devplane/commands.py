from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from .core import DevPlaneError


def describe_dirty(status: str, limit: int = 5) -> str:
    """Name the first paths of ``git status --porcelain`` output for an error message.

    Returns an empty string for a clean status, otherwise ``": ?? a.txt, M b.py"``.
    """
    entries = [
        f"{line[:2].strip()} {line[3:]}".strip() for line in status.splitlines() if line.strip()
    ]
    if not entries:
        return ""
    shown = ", ".join(entries[:limit])
    extra = len(entries) - limit
    return f": {shown}" + (f" (and {extra} more)" if extra > 0 else "")


def run_checked(
    args: list[str],
    cwd: Path,
    timeout: int = 900,
    *,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: UP022 - explicit streams are part of the runner contract
            args,
            cwd=cwd,
            check=True,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise DevPlaneError(f"required executable not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DevPlaneError(
            f"command {args[0]!r} timed out after {timeout} seconds"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise DevPlaneError(
            f"command {args[0]!r} failed with exit code {exc.returncode}"
        ) from exc
