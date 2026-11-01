"""The Claude Agent SDK worker plugin (`factory.workers.claude`): runs the `claude` CLI.

The SDK (and the `claude` CLI under it) reads its endpoint, credential and model from
environment variables, so the call is launched with `claude_env` merged over a clean
environment. What the agent may do is Claude's tool allowlist (`claude_allowlist`).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from .. import process
from ..config import Config, ConfigError
from .base import Tools, Usage, check_reply, reject_options, usage

PLUGIN = "factory.workers.claude"
CLAUDE_TIMEOUT = 1500  # seconds


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


def _budget_flag(cfg: Config) -> list[str]:
    """The per-call cost cap, when llm.yaml sets one. Without it the call has no cap."""
    cap = cfg.budgets.per_task_usd
    return ["--max-budget-usd", str(cap)] if cap is not None else []


def claude_allowlist(tools: Tools) -> list[str]:
    return ["Read", "Glob", "Grep",
            *(["Edit", "Write"] if tools.edit else []),
            *(["WebFetch"] if tools.web else []),
            *(f"Bash({c}:*)" for c in tools.bash)]


def claude_env(
    cfg: Config,
    worker_name: str,
    *,
    model: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    worker = cfg.worker(worker_name)
    if worker.plugin != PLUGIN:
        raise ConfigError(
            f"worker {worker_name!r} uses plugin {worker.plugin!r}, "
            f"not 'claude-agent-sdk' ({PLUGIN})"
        )
    chosen = cfg.check_request(worker_name, model)
    key = environ.get(worker.api_key_env)
    if not key:
        raise ConfigError(f"environment variable {worker.api_key_env} is not set")
    return {
        "ANTHROPIC_BASE_URL": worker.base_url,
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_MODEL": chosen,
    }


class Plugin:
    name = "claude-agent-sdk"

    def __init__(self, cfg: Config, worker: str) -> None:
        self.cfg = cfg
        self.worker = worker

    @classmethod
    def validate(cls, cfg: Config, worker: str) -> None:
        reject_options(cfg, worker, cls.name)

    def run(self, prompt: str, cwd: Path, tools: Tools, model: str) -> tuple[str, Usage]:
        # A clean environment: a session's CLAUDE_CODE_* variables hang the nested call.
        env = {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"],
               **claude_env(self.cfg, self.worker, model=model)}
        argv = ["claude", "-p", prompt, "--model", model, "--output-format", "json",
                "--permission-mode", "acceptEdits", *_budget_flag(self.cfg),
                "--allowedTools", *claude_allowlist(tools)]
        r = process.run(argv, cwd=cwd, env=env, timeout=CLAUDE_TIMEOUT)
        text, spent = claude_result(r.stdout, self.worker, model)
        return check_reply(self.worker, r, text, spent), spent
