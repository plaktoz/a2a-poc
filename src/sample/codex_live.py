"""Live call to a Codex worker through the gateway, built from llm.yaml.

Usage: uv run python src/sample/codex_live.py [worker]   (default: codex-gpt)
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from factory import codex_argv, load_config

CONFIG = Path(__file__).resolve().parents[2] / "llm.yaml"


def main() -> int:
    worker = sys.argv[1] if len(sys.argv) > 1 else "codex-gpt"
    cfg = load_config(CONFIG)
    with tempfile.TemporaryDirectory() as workdir:
        argv = codex_argv(cfg, worker, "Reply with exactly: OK", sandbox="read-only", cd=workdir)
        argv.insert(argv.index("--"), "--skip-git-repo-check")  # the temp dir is not a repo
        # stdin must be closed, or `codex exec` waits on it and hangs.
        result = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90
        )
    print(f"worker={worker} exit={result.returncode}")
    print(result.stdout[-300:].strip())
    if result.returncode != 0:
        print(result.stderr[-600:].strip(), file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
