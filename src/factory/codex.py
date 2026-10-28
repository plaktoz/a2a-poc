"""Build the `codex exec` command for a Codex worker from llm.yaml.

The factory never reads `~/.codex/config.toml`: `--ignore-user-config` skips it
and the provider block is passed as `-c` overrides. Codex reads the API key
from the environment variable named by `env_key`, so the key is never on the
command line.

Callers must run the command with stdin closed (`stdin=subprocess.DEVNULL`);
`codex exec` otherwise waits on an open stdin and hangs.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config, ConfigError


def _toml_str(value: str) -> str:
    # A JSON string literal is a valid TOML basic string.
    return json.dumps(value)


def codex_argv(
    cfg: Config,
    worker_name: str,
    prompt: str,
    *,
    model: str | None = None,
    sandbox: str = "workspace-write",
    cd: str | Path | None = None,
) -> list[str]:
    worker = cfg.worker(worker_name)
    if worker.harness != "codex":
        raise ConfigError(f"worker {worker_name!r} uses harness {worker.harness!r}, not 'codex'")
    chosen = cfg.check_request(worker_name, model)

    provider = f"factory-{worker.name}"
    prefix = f"model_providers.{provider}"
    argv = [
        "codex",
        "exec",
        "--ignore-user-config",
        "-s",
        sandbox,
        "-m",
        chosen,
        "-c",
        f"model_provider={_toml_str(provider)}",
        "-c",
        f"{prefix}.name={_toml_str(provider)}",
        "-c",
        f"{prefix}.base_url={_toml_str(worker.base_url)}",
        "-c",
        f"{prefix}.env_key={_toml_str(worker.api_key_env)}",
        "-c",
        f"{prefix}.wire_api={_toml_str(worker.wire_api)}",
    ]
    if cd is not None:
        argv += ["-C", str(cd)]
    argv += ["--", prompt]
    return argv
