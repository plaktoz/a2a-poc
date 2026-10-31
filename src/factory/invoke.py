"""Run one LLM worker on a prompt in a directory and return its final reply.

The worker is whatever llm.yaml says (a Claude Agent SDK or Codex harness). What the agent may
do is a `Tools` value, mapped onto each harness's own controls: Claude's tool allowlist, or
the Codex sandbox. Codex has no per-command allowlist, so the orchestrator also checks which
files an agent changed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .claude import claude_env
from .codex import codex_argv
from .config import Config, ConfigError
from .process import run, tail

CODEX_TIMEOUT, CLAUDE_TIMEOUT = 900, 1500


@dataclass(frozen=True)
class Tools:
    """`edit`: write files. `web`: Claude's WebFetch (WebSearch is not offered: the gateway
    rejects it). `bash`: command prefixes the agent may run, such as "npx vitest run" or "curl".
    `network`: commands may reach the internet (opens the Codex sandbox's network).
    """

    edit: bool = False
    web: bool = False
    bash: tuple[str, ...] = field(default=())
    network: bool = False


def claude_allowlist(tools: Tools) -> list[str]:
    return ["Read", "Glob", "Grep",
            *(["Edit", "Write"] if tools.edit else []),
            *(["WebFetch"] if tools.web else []),
            *(f"Bash({c}:*)" for c in tools.bash)]


def invoke(cfg: Config, worker: str, model: str, prompt: str, cwd: Path, tools: Tools) -> str:
    w = cfg.worker(worker)
    if w.harness == "codex":
        writable = tools.edit or bool(tools.bash)
        argv = codex_argv(cfg, worker, prompt, model=model,
                          sandbox="workspace-write" if writable else "read-only", cd=cwd)
        i = argv.index("--")
        argv[i:i] = ["--skip-git-repo-check"]
        if tools.network and writable:
            argv[i:i] = ["-c", "sandbox_workspace_write.network_access=true"]
        r = run(argv, cwd=cwd, timeout=CODEX_TIMEOUT)
    elif w.harness == "claude-agent-sdk":
        # A clean environment: a session's CLAUDE_CODE_* variables hang the nested call.
        env = {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"],
               **claude_env(cfg, worker, model=model)}
        argv = ["claude", "-p", prompt, "--model", model, "--permission-mode", "acceptEdits",
                "--max-budget-usd", str(cfg.budgets.per_task_usd),
                "--allowedTools", *claude_allowlist(tools)]
        r = run(argv, cwd=cwd, env=env, timeout=CLAUDE_TIMEOUT)
    else:
        raise ConfigError(f"worker {worker!r}: harness {w.harness!r} is not supported")
    if r.returncode != 0:
        raise RuntimeError(f"{worker} exited {r.returncode}: {tail(r, 500)}")
    return r.stdout
