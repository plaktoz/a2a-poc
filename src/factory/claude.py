"""Build the environment for a Claude Agent SDK worker from llm.yaml.

The SDK (and the `claude` CLI under it) reads its endpoint, credential and
model from these environment variables, so the worker's process is launched
with the returned mapping merged over its inherited environment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from .config import Config, ConfigError


def claude_env(
    cfg: Config,
    worker_name: str,
    *,
    model: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    worker = cfg.worker(worker_name)
    if worker.harness != "claude-agent-sdk":
        raise ConfigError(
            f"worker {worker_name!r} uses harness {worker.harness!r}, not 'claude-agent-sdk'"
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
