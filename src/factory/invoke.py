"""Run one LLM worker on a prompt in a directory and return its final reply.

The worker is whatever llm.yaml says: each worker names a plugin by module path, and
`invoke` runs that plugin. The built-in plugins are `factory.workers.claude` (Claude Agent
SDK) and `factory.workers.codex`. The plugin interface is in `workers/base.py`.

What the agent may do is a `Tools` value; each plugin maps it onto its own controls.
Codex has no per-command allowlist, so the orchestrator also checks which files an agent
changed.

Each call also returns its usage: the worker, the model, the tokens and the cost. Claude
reports its cost. Codex reports tokens only, so its cost is None.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .workers.base import LLMFailed, Tools, Usage, usage
from .workers.claude import claude_result
from .workers.codex import codex_result
from .workers.loader import load_plugin

__all__ = [
    "LLMFailed",
    "Tools",
    "Usage",
    "claude_result",
    "codex_result",
    "invoke",
    "usage",
]


def invoke(cfg: Config, worker: str, model: str, prompt: str, cwd: Path,
           tools: Tools) -> tuple[str, Usage]:
    """The worker's final reply and the call's usage. Raises LLMFailed, with the usage, on a failure."""
    return load_plugin(cfg, worker).run(prompt, cwd, tools, model)
