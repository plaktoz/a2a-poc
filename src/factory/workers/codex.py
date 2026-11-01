"""The Codex worker plugin (`factory.workers.codex`): runs `codex exec`.

The factory never reads `~/.codex/config.toml`: `--ignore-user-config` skips it
and the provider block is passed as `-c` overrides. Codex reads the API key
from the environment variable named by `env_key`, so the key is never on the
command line. Codex has no per-command allowlist; the sandbox is its control
(`read-only` unless the agent may edit or run commands).

`process.run` closes stdin: `codex exec` otherwise waits on an open stdin and hangs.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import process
from ..config import Config, ConfigError
from .base import Tools, Usage, check_reply, reject_options, usage

PLUGIN = "factory.workers.codex"
CODEX_TIMEOUT = 900  # seconds


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
    if worker.plugin != PLUGIN:
        raise ConfigError(
            f"worker {worker_name!r} uses plugin {worker.plugin!r}, not 'codex' ({PLUGIN})"
        )
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


class Plugin:
    name = "codex"

    def __init__(self, cfg: Config, worker: str) -> None:
        self.cfg = cfg
        self.worker = worker

    @classmethod
    def validate(cls, cfg: Config, worker: str) -> None:
        reject_options(cfg, worker, cls.name)

    def run(self, prompt: str, cwd: Path, tools: Tools, model: str) -> tuple[str, Usage]:
        writable = tools.edit or bool(tools.bash)
        argv = codex_argv(self.cfg, self.worker, prompt, model=model,
                          sandbox="workspace-write" if writable else "read-only", cd=cwd)
        i = argv.index("--")
        argv[i:i] = ["--skip-git-repo-check", "--json"]
        if tools.network and writable:
            argv[i:i] = ["-c", "sandbox_workspace_write.network_access=true"]
        r = process.run(argv, cwd=cwd, timeout=CODEX_TIMEOUT)
        text, spent = codex_result(r.stdout, self.worker, model)
        return check_reply(self.worker, r, text, spent), spent
