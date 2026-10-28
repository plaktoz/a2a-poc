"""Live call to the Claude worker through the gateway, built from llm.yaml.

Usage: uv run python src/sample/claude_live.py [worker]   (default: claude-agent-sdk)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from factory import claude_env, load_config

CONFIG = Path(__file__).resolve().parents[2] / "llm.yaml"


def main() -> int:
    worker = sys.argv[1] if len(sys.argv) > 1 else "claude-agent-sdk"
    cfg = load_config(CONFIG)
    # Clean environment: only what the factory sets. Inheriting a Claude Code
    # session's CLAUDE_CODE_* variables made the nested call hang.
    env = {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"], **claude_env(cfg, worker)}
    with tempfile.TemporaryDirectory() as workdir:
        result = subprocess.run(
            ["claude", "-p", "Reply with exactly: OK", "--model", env["ANTHROPIC_MODEL"]],
            env=env,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=90,
        )
    print(f"worker={worker} exit={result.returncode}")
    print(result.stdout[-300:].strip())
    if result.returncode != 0:
        print(result.stderr[-600:].strip(), file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
