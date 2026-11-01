"""Run external commands: stdin closed, output captured, bounded time."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

CMD_TIMEOUT = 300


def run(argv: list[str], *, cwd: Path, env: Mapping[str, str] | None = None,
        timeout: int = CMD_TIMEOUT) -> subprocess.CompletedProcess[str]:
    # stdin is closed: `codex exec` hangs on an open stdin.
    return subprocess.run(
        argv, cwd=cwd, env=dict(env) if env is not None else None, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout,
    )


def tail(r: subprocess.CompletedProcess[str], n: int = 2000) -> str:
    return (r.stdout + r.stderr)[-n:]
