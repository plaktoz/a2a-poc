"""Run one LLM worker on a prompt in a directory and return its final reply.

The worker is whatever llm.yaml says: each worker names a plugin by module path, and
`invoke` runs that plugin. The built-in plugins are `factory.workers.claude` (Claude Agent
SDK) and `factory.workers.codex`. The plugin interface is in `workers/base.py`.

What the agent may do is a `Tools` value; each plugin maps it onto its own controls.
Codex has no per-command allowlist, so the orchestrator also checks which files an agent
changed.

Each call also returns its usage: the worker, the model, the tokens and the cost. Claude
reports its cost. Codex reports tokens only, so `invoke` prices it from docs/model-prices.md.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import prices
from .config import Config
from .workers.base import LLMFailed, Tools, Usage, usage
from .workers.claude import claude_result
from .workers.codex import codex_result
from .workers.loader import load_plugin

log = logging.getLogger(__name__)

__all__ = [
    "LLMFailed",
    "Tools",
    "Usage",
    "claude_result",
    "codex_result",
    "invoke",
    "price_usage",
    "usage",
]


def invoke(cfg: Config, worker: str, model: str, prompt: str, cwd: Path,
           tools: Tools) -> tuple[str, Usage]:
    """The worker's final reply and the call's usage. Raises LLMFailed, with the usage, on a failure.

    A call whose worker reports no cost gets it from docs/model-prices.md (FR-30). A model with no
    price there keeps `cost_usd` None, which the run total counts as unknown.
    """
    try:
        text, spent = load_plugin(cfg, worker).run(prompt, cwd, tools, model)
    except LLMFailed as e:
        price_usage(e.usage)
        raise
    return text, price_usage(spent)


def price_usage(spent: Usage, table: dict | None = None) -> Usage:
    """Fill `cost_usd` from the price table when the worker gave none. Changes `spent` and returns it."""
    if spent.get("cost_usd") is None and (spent["input_tokens"] or spent["output_tokens"]):
        try:
            spent["cost_usd"] = prices.cost_usd(spent["model"], spent["input_tokens"],
                                                spent["cached_input_tokens"], spent["output_tokens"], table)
        except prices.PriceError as e:  # a broken price file must not fail a call that worked
            log.warning("no cost for %s: %s", spent["model"], e)
    return spent
