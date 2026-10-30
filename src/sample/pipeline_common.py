"""Shared by the A2A orchestrator (pipeline.py) and the stage agents (stage_agents.py)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from factory import ConfigError, FactoryError

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
APPS = ROOT / "apps"  # one directory per built app, so builds never share files
TEMPLATE = ROOT / "templates" / "react-vite"  # scaffold copied into each new app
DEFAULT_STAGES = HERE / "pipeline.yaml"
STAGE_NAMES = ("requirements", "tests", "implement", "verify")

TEST_FILE = "src/App.test.jsx"
CMD_TIMEOUT = 300


@dataclass(frozen=True)
class Stage:
    name: str
    worker: str
    model: str  # resolved, and checked against llm.yaml
    url: str  # where the stage's A2A agent serves


def load_stages(cfg, path: Path) -> dict[str, Stage]:
    data = yaml.safe_load(path.read_text()) or {}
    entries = data.get("stages")
    if not isinstance(entries, dict) or set(entries) != set(STAGE_NAMES):
        raise ConfigError(f"{path}: `stages` must have exactly these keys: {', '.join(STAGE_NAMES)}")
    stages = {}
    for name in STAGE_NAMES:
        e = entries[name]
        if not isinstance(e, dict) or set(e) - {"worker", "model", "url"} or not {"worker", "url"} <= set(e):
            raise ConfigError(f"{path}: stage {name!r} needs `worker` and `url`, and may set `model`")
        u = urlsplit(e["url"])
        if u.scheme != "http" or not u.hostname or not u.port:
            raise ConfigError(f"{path}: stage {name!r}: url must be http://host:port, got {e['url']!r}")
        try:
            model = cfg.check_request(e["worker"], e.get("model"))
        except FactoryError as err:
            raise ConfigError(f"{path}: stage {name!r}: {err}") from err
        stages[name] = Stage(name, e["worker"], model, e["url"].rstrip("/"))
    urls = [s.url for s in stages.values()]
    if len(set(urls)) != len(urls):
        raise ConfigError(f"{path}: each stage needs its own url")
    return stages


def run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
        timeout: int = CMD_TIMEOUT) -> subprocess.CompletedProcess[str]:  # fmt: skip
    # stdin is closed: `codex exec` hangs on an open stdin.
    return subprocess.run(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout,
    )  # fmt: skip


def tail(r: subprocess.CompletedProcess[str], n: int = 2000) -> str:
    return (r.stdout + r.stderr)[-n:]


def code_block(text: str, langs: str) -> str | None:
    blocks = re.findall(rf"```(?:{langs})\n(.*?)```", text, re.S)
    return blocks[-1] if blocks else None


def check_spec(spec: dict) -> dict:
    """Return the spec with optional lists defaulted; raise if it is not usable."""
    if not isinstance(spec, dict) or not spec.get("title") or not spec.get("goal"):
        raise ValueError("spec needs a title and a goal")
    criteria = spec.get("acceptance_criteria") or []
    # FR-3: a spec without clear acceptance criteria goes back to its author.
    if not criteria or any(not isinstance(c, dict) or not c.get("text") for c in criteria):
        raise ValueError("spec has missing or empty acceptance criteria")
    if [c.get("id") for c in criteria] != [f"AC{i}" for i in range(1, len(criteria) + 1)]:
        raise ValueError("acceptance criteria ids must be AC1, AC2, ... in order")
    for key in ("constraints", "out_of_scope", "assumptions", "questions"):
        spec.setdefault(key, [])
    return spec


def spec_text(spec: dict) -> str:
    lines = [f"# {spec['title']}", "", spec["goal"], "", "Acceptance criteria:"]
    lines += [f"  {c['id']}: {c['text']}" for c in spec["acceptance_criteria"]]
    for key in ("constraints", "out_of_scope", "assumptions"):
        if spec.get(key):
            lines += ["", key.replace("_", " ").capitalize() + ":", *(f"  - {x}" for x in spec[key])]
    return "\n".join(lines)
