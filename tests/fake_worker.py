"""A fake worker plugin for tests/test_workers.py. Named in llm.yaml as `plugin: fake_worker`.

It records each call in CALLS, replies with the `reply` option (default "fake reply") and
spends a fixed usage. A prompt of "fail" raises LLMFailed with that usage.
"""

from pathlib import Path

from factory.config import Config, ConfigError
from factory.workers.base import LLMFailed, Tools, Usage, usage

CALLS: list[dict] = []
SPENT = dict(input_tokens=7, cached=3, output=2, cost=0.02)


class Plugin:
    name = "fake"

    def __init__(self, cfg: Config, worker: str) -> None:
        self.cfg = cfg
        self.worker = worker
        self.reply = cfg.worker(worker).options.get("reply", "fake reply")

    @classmethod
    def validate(cls, cfg: Config, worker: str) -> None:
        options = cfg.worker(worker).options
        extra = set(options) - {"reply"}
        if extra:
            raise ConfigError(f"worker {worker!r}: fake plugin has no option {sorted(extra)}")
        if "reply" in options and not isinstance(options["reply"], str):
            raise ConfigError(f"worker {worker!r}: fake option 'reply' must be a string")

    def run(self, prompt: str, cwd: Path, tools: Tools, model: str) -> tuple[str, Usage]:
        CALLS.append({"worker": self.worker, "prompt": prompt, "cwd": cwd, "tools": tools,
                      "model": model})
        spent = usage(self.worker, model, SPENT["input_tokens"], SPENT["cached"], SPENT["output"],
                      SPENT["cost"])
        if prompt == "fail":
            raise LLMFailed("fake failed", spent)
        return self.reply, spent
