from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from .core import DevPlaneError


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
