"""The run's agents: one A2A server per role the plan uses, each on its own free port.

The orchestrator talks to them only through A2A. It checks each agent's card says the role,
worker and model the plan staffs, so a stale or foreign server on a port is never used. A role
staffed with a `url` in agents.yaml is an agent someone else runs; it is used as is.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .a2a_transport import Json, call, fetch_card
from .gates import free_port
from .paths import LLM_CONFIG
from .runbook import Staff

START_TIMEOUT = 30


class Fleet:
    def __init__(self, staff: dict[str, Staff], roles: list[str], log_dir: Path,
                 config: Path = LLM_CONFIG) -> None:
        self.staff = {r: staff[r] for r in roles}
        self.log_dir, self.config = log_dir, config
        self.urls: dict[str, str] = {}
        self.procs: list[subprocess.Popen] = []

    def __enter__(self) -> Fleet:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        try:
            for role, st in self.staff.items():
                if st.url:
                    self.urls[role] = st.url
                    continue
                port = free_port()
                self.urls[role] = f"http://127.0.0.1:{port}"
                argv = [sys.executable, "-m", "factory.agent", role, "--worker", st.worker,
                        "--model", st.model, "--port", str(port), "--config", str(self.config)]
                self.procs.append(subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL, stdout=(self.log_dir / f"{role}.log").open("w"),
                    stderr=subprocess.STDOUT))
            for role in self.staff:
                self._wait(role)
        except BaseException:
            self.__exit__()
            raise
        return self

    def _wait(self, role: str) -> None:
        st, url = self.staff[role], self.urls[role]
        deadline = time.monotonic() + START_TIMEOUT
        while True:
            try:
                card = fetch_card(url)
                break
            except Exception as e:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"no A2A agent card at {url} for role {role} "
                                       f"(see {self.log_dir / (role + '.log')}): {e}") from e
                time.sleep(0.3)
        tags = set(card["skills"][0].get("tags", []))
        want = {f"role:{role}", f"worker:{st.worker}", f"model:{st.model}"}
        if not want <= tags:
            raise RuntimeError(f"the agent at {url} advertises {sorted(tags)}, expected {sorted(want)}")

    def __exit__(self, *exc) -> None:
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    def send(self, role: str, request: Json, on_input: Callable[[Json], Json] | None = None) -> Json:
        return asyncio.run(call(self.urls[role], request, on_input))

    def describe(self) -> list[str]:
        return [f"{r:<10} {self.urls[r]}  {s.worker} / {s.model}" for r, s in self.staff.items()]
