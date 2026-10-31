"""Run one LLM worker on a prompt in a directory and return its final reply.

The worker is whatever llm.yaml says (a Claude Agent SDK or Codex harness). What the agent may
do is a `Tools` value, mapped onto each harness's own controls: Claude's tool allowlist, or
the Codex sandbox. Codex has no per-command allowlist, so the orchestrator also checks which
files an agent changed.

Each call also returns its usage: the worker, the model, the tokens and the cost. Both CLIs
print JSON for this. Claude reports its cost. Codex reports tokens only, so its cost is None.
`input_tokens` counts every prompt token, cached or not, so the two harnesses compare.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .claude import claude_env
from .codex import codex_argv
from .config import Config, ConfigError
from .process import run, tail

CODEX_TIMEOUT, CLAUDE_TIMEOUT = 900, 1500

Usage = dict[str, Any]  # {worker, model, input_tokens, cached_input_tokens, output_tokens, cost_usd}


class LLMFailed(RuntimeError):
    """A worker call that failed. `usage` is what the call spent before it failed."""

    def __init__(self, message: str, usage: Usage) -> None:
        super().__init__(message)
        self.usage = usage


def usage(worker: str, model: str, input_tokens: int = 0, cached: int = 0, output: int = 0,
          cost: float | None = None) -> Usage:
    return {"worker": worker, "model": model, "input_tokens": input_tokens,
            "cached_input_tokens": cached, "output_tokens": output, "cost_usd": cost}


def claude_result(stdout: str, worker: str, model: str) -> tuple[str | None, Usage]:
    """The reply and usage in `claude -p --output-format json` output. The reply is None on an error."""
    try:
        d = json.loads(stdout)
    except ValueError:
        return None, usage(worker, model)
    if not isinstance(d, dict):
        return None, usage(worker, model)
    # modelUsage has the counts even when the call ended in an error; `usage` can be all zero then.
    if per_model := [m for m in (d.get("modelUsage") or {}).values() if isinstance(m, dict)]:
        new, read, write, out = (sum(int(m.get(k) or 0) for m in per_model) for k in (
            "inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens"))
    else:
        u = d.get("usage") or {}
        new, read, write, out = (int(u.get(k) or 0) for k in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
    cost = d.get("total_cost_usd")
    text = None if d.get("is_error") else d.get("result")
    return text, usage(worker, model, new + read + write, read, out,
                       float(cost) if cost is not None else None)


def codex_result(stdout: str, worker: str, model: str) -> tuple[str, Usage]:
    """The last agent message and the summed token counts in `codex exec --json` output."""
    text, counts = "", {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    for line in stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        item = e.get("item") or {}
        # Other items, such as the "model metadata not found" error, are not the reply.
        if e.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text") or ""
        elif e.get("type") == "turn.completed":
            u = e.get("usage") or {}
            for k in counts:
                counts[k] += int(u.get(k) or 0)
    return text, usage(worker, model, counts["input_tokens"], counts["cached_input_tokens"],
                       counts["output_tokens"])


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


def invoke(cfg: Config, worker: str, model: str, prompt: str, cwd: Path,
           tools: Tools) -> tuple[str, Usage]:
    """The worker's final reply and the call's usage. Raises LLMFailed, with the usage, on a failure."""
    w = cfg.worker(worker)
    if w.harness == "codex":
        writable = tools.edit or bool(tools.bash)
        argv = codex_argv(cfg, worker, prompt, model=model,
                          sandbox="workspace-write" if writable else "read-only", cd=cwd)
        i = argv.index("--")
        argv[i:i] = ["--skip-git-repo-check", "--json"]
        if tools.network and writable:
            argv[i:i] = ["-c", "sandbox_workspace_write.network_access=true"]
        r = run(argv, cwd=cwd, timeout=CODEX_TIMEOUT)
        text, spent = codex_result(r.stdout, worker, model)
    elif w.harness == "claude-agent-sdk":
        # A clean environment: a session's CLAUDE_CODE_* variables hang the nested call.
        env = {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"],
               **claude_env(cfg, worker, model=model)}
        argv = ["claude", "-p", prompt, "--model", model, "--output-format", "json",
                "--permission-mode", "acceptEdits", "--max-budget-usd", str(cfg.budgets.per_task_usd),
                "--allowedTools", *claude_allowlist(tools)]
        r = run(argv, cwd=cwd, env=env, timeout=CLAUDE_TIMEOUT)
        text, spent = claude_result(r.stdout, worker, model)
    else:
        raise ConfigError(f"worker {worker!r}: harness {w.harness!r} is not supported")
    if r.returncode != 0 or text is None:
        raise LLMFailed(f"{worker} exited {r.returncode}: {tail(r, 500)}", spent)
    return text, spent
