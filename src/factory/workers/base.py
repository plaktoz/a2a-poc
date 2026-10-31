"""The worker plugin interface, and the types every plugin shares.

`llm.yaml` names each worker's plugin by module path (`plugin: factory.workers.codex`).
`load_config` imports every plugin at start (see `loader.py`), and `invoke` runs the one
the worker names. A plugin module defines a class `Plugin` with:

- `name`: a short str, such as "codex". Messages use it.
- `__init__(self, cfg, worker)`: bind the plugin to one worker of the config.
- `validate(cfg, worker)`, a classmethod: raise ConfigError if the worker's config does not
  suit the plugin. `reject_options` covers a plugin that takes no `options`.
- `run(self, prompt, cwd, tools, model)`: run the worker once and return `(text, Usage)`.
  On a failure, raise `LLMFailed` with the usage the call spent.

Every call reports its usage: the worker, the model, the tokens and the cost (CLAUDE.md rule 1).
`input_tokens` counts every prompt token, cached or not, so the harnesses compare.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any, Protocol

from ..config import Config, ConfigError
from ..process import tail

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


class WorkerPlugin(Protocol):
    """The shape of every plugin's `Plugin` class. See the module docstring."""

    name: str

    def __init__(self, cfg: Config, worker: str) -> None: ...

    @classmethod
    def validate(cls, cfg: Config, worker: str) -> None: ...

    def run(self, prompt: str, cwd: Path, tools: Tools, model: str) -> tuple[str, Usage]: ...


def check_reply(worker: str, r: CompletedProcess[str], text: str | None, spent: Usage) -> str:
    """The reply, or LLMFailed (with the usage) when the process failed or gave no reply."""
    if r.returncode != 0 or text is None:
        raise LLMFailed(f"{worker} exited {r.returncode}: {tail(r, 500)}", spent)
    return text


def reject_options(cfg: Config, worker: str, plugin: str) -> None:
    """For a plugin that takes no `options`: raise ConfigError if the worker sets any."""
    if options := cfg.worker(worker).options:
        raise ConfigError(
            f"worker {worker!r}: plugin {plugin!r} takes no options; "
            f"remove {', '.join(sorted(options))}"
        )
