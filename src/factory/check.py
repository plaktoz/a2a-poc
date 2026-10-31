"""`factory check`: send one short prompt to every enabled worker and model.

It runs before a long job, so a dead model, a wrong key or a missing CLI shows up in
seconds, not in the middle of a run. It calls the same `invoke()` path that the agents use,
so a pass means the factory can really reach the model. It spends a few tokens per model.

The check reports the usage of each call, so the cost of the check is visible (CLAUDE.md rule 1).
"""

from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config, ModelNotAllowed
from .invoke import LLMFailed, Tools, invoke

PROMPT = "Reply with the single word: pong"
CHECK_TIMEOUT = 120  # seconds, per model


@dataclass
class Result:
    worker: str
    model: str
    ok: bool
    seconds: float
    detail: str  # the reply on a pass, the error on a failure
    usage: dict | None = None


def check_one(cfg: Config, worker: str, model: str, cwd: Path) -> Result:
    start = time.monotonic()
    try:
        cfg.check_request(worker, model)  # every plugin must refuse a model the config does not allow
        text, spent = invoke(cfg, worker, model, PROMPT, cwd, Tools())
    except LLMFailed as e:
        return Result(worker, model, False, time.monotonic() - start, str(e)[:300], e.usage)
    except ModelNotAllowed as e:
        return Result(worker, model, False, time.monotonic() - start, str(e), None)
    except Exception as e:  # a timeout or a process that cannot start
        return Result(worker, model, False, time.monotonic() - start,
                      f"{type(e).__name__}: {e}"[:300], None)
    reply = " ".join(text.split())[:80]
    return Result(worker, model, True, time.monotonic() - start, reply, spent)


def check_all(cfg: Config, cwd: Path, workers: list[str] | None = None) -> list[Result]:
    """Check each enabled worker's models. The calls run in parallel, one per model."""
    pairs = [(w.name, m) for w in cfg.enabled_workers() for m in w.models
             if workers is None or w.name in workers]
    if not pairs:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(pairs)) as pool:
        futures = [pool.submit(check_one, cfg, w, m, cwd) for w, m in pairs]
        results = []
        for (w, m), fut in zip(pairs, futures):
            try:
                results.append(fut.result(timeout=CHECK_TIMEOUT))
            except concurrent.futures.TimeoutError:
                results.append(Result(w, m, False, CHECK_TIMEOUT,
                                      f"no reply within {CHECK_TIMEOUT} seconds"))
    return results


def render(results: list[Result]) -> str:
    if not results:
        return "No enabled worker to check."
    lines = []
    for r in results:
        mark = "ok  " if r.ok else "FAIL"
        lines.append(f"{mark} {r.worker} / {r.model} ({r.seconds:.1f}s): {r.detail}")
    passed = sum(r.ok for r in results)
    cost = sum((r.usage or {}).get("cost_usd") or 0.0 for r in results)
    tokens = sum((r.usage or {}).get("input_tokens", 0) + (r.usage or {}).get("output_tokens", 0)
                 for r in results)
    lines.append(f"\n{passed}/{len(results)} models answered. Check cost: "
                 f"{tokens} tokens, ${cost:.4f} (codex cost is not reported).")
    return "\n".join(lines)
