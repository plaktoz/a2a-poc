"""Build an app from your requirements through four A2A agents, one per stage.

  1. requirements  asks you questions and drafts a spec; you accept it
  2. tests         writes the acceptance tests                        gate: suite loads, RED
  3. implement     builds the app                                     gate: builds and serves
  4. verify        runs the tests, checks each criterion              gate: GREEN (cross-checked),
                                                                            every criterion PASS

The orchestrator only talks A2A: it reads each agent's card, sends it a task and
receives the result. Which LLM worker and model each agent uses, and its url, is set in
pipeline.yaml. The orchestrator owns the gates: after every stage it checks which files the
agent changed, and runs Vitest itself instead of trusting a reported count. A failure in
stage 3 or 4 goes back to the developer, up to 1 + budgets.max_retries rounds.

Every app is built in its own directory, apps/<name>/, copied from templates/react-vite, and
each run starts its own agents on free ports. Runs for different apps do not share files,
ports or logs, so you can start several at once, each in its own terminal.

Usage (from the repo root):
  uv run python src/sample/pipeline.py --app todo --brief "a todo app with due dates"
  uv run python src/sample/pipeline.py --app todo --rebuild   # build over an existing app
  uv run python src/sample/pipeline.py --app todo --yes       # accept the first spec without questions
  uv run python src/sample/pipeline.py --app todo --spec PATH.json  # skip stage 1, reuse a spec
  uv run python src/sample/pipeline.py --stages OTHER.yaml    # other workers, models or urls
  uv run python src/sample/pipeline.py --no-launch            # agents are already running

--app defaults to kanban-react for the default brief, and to a name made from --brief otherwise.
Writes SPEC.json and src/ in apps/<name>/, and a run log and the agent logs in
apps/<name>/.factory/. An app that already has a SPEC.json is only rebuilt with --rebuild, and
two runs of the same app cannot overlap.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from factory import ConfigError, load_config
from factory.a2a_transport import StageFailed, call, fetch_card
from pipeline_common import (
    APPS, DEFAULT_STAGES, HERE, ROOT, STAGE_NAMES, TEMPLATE, TEST_FILE, Stage,
    check_spec, load_stages, run, spec_text, tail,
)  # fmt: skip

DEFAULT_APP = "kanban-react"
APP_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
DEFAULT_BRIEF = (
    "A project management kanban board web app. I can create tickets, and add new "
    "statuses (columns) to the board as I need them."
)
MAX_SPEC_ROUNDS = 6
AGENT_START_TIMEOUT = 30

RESULT_FILE = ".vitest-result.json"
# Scaffold files no agent may change. Everything else under src/ is generated.
PROTECTED = {"package.json", "package-lock.json", "vite.config.js", "index.html",
             "src/main.jsx", "src/setupTests.js"}  # fmt: skip
IGNORED_DIRS = {"node_modules", "dist", ".factory"}
APP_SKELETON = "export default function App() {\n  return null\n}\n"


def step(msg: str) -> None:
    print(msg, flush=True)


# --- agents -----------------------------------------------------------------


class Agents:
    """Starts the stage agents (unless they are already up) and sends them tasks over A2A."""

    def __init__(self, stages: dict[str, Stage], stages_file: Path, launch: bool,
                 log_dir: Path) -> None:  # fmt: skip
        if launch:
            # This run's own agents, each on a port nobody else holds, so runs do not collide.
            stages = {k: replace(s, url=f"http://127.0.0.1:{free_port()}") for k, s in stages.items()}
        self.stages, self.stages_file, self.launch, self.log_dir = stages, stages_file, launch, log_dir
        self.procs: list[subprocess.Popen] = []
        self.cards: dict[str, dict] = {}

    def __enter__(self) -> Agents:
        logs = self.log_dir
        logs.mkdir(parents=True, exist_ok=True)
        try:
            for s in self.stages.values():
                if self.launch:
                    self.procs.append(subprocess.Popen(
                        [sys.executable, str(HERE / "stage_agents.py"), s.name,
                         "--stages", str(self.stages_file), "--port", str(urlsplit(s.url).port)],
                        stdin=subprocess.DEVNULL, stdout=(logs / f"{s.name}.log").open("w"),
                        stderr=subprocess.STDOUT,
                    ))  # fmt: skip
            for s in self.stages.values():
                self.cards[s.name] = self._wait_for_card(s)
        except BaseException:
            self.__exit__()
            raise
        return self

    def _wait_for_card(self, s: Stage) -> dict:
        deadline = time.monotonic() + AGENT_START_TIMEOUT
        while True:
            try:
                card = fetch_card(s.url)
                break
            except Exception as e:
                if time.monotonic() > deadline:
                    hint = f"see {self.log_dir / (s.name + '.log')}" if self.launch else "is it running?"
                    raise RuntimeError(f"no A2A agent card at {s.url} for stage {s.name} ({hint}): {e}") from e
                time.sleep(0.3)
        # The card must be the agent this stage is configured for, not something else on the port.
        tags = set(card["skills"][0].get("tags", []))
        want = {f"stage:{s.name}", f"worker:{s.worker}", f"model:{s.model}"}
        if not want <= tags:
            raise RuntimeError(f"agent at {s.url} advertises {sorted(tags)}, expected {sorted(want)}")
        return card

    def __exit__(self, *exc) -> None:
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    def send(self, stage: str, request: dict, on_input=None) -> dict:
        return asyncio.run(call(self.stages[stage].url, request, on_input))

    def describe(self, stage: str) -> str:
        s, card = self.stages[stage], self.cards[stage]
        return f"{card['name']} @ {s.url} ({s.worker} / {s.model})"


# --- gates (run by the orchestrator, not the agents) ------------------------


def snapshot(workdir: Path) -> dict[str, str]:
    """sha256 of every project file, keyed by path relative to the workdir."""
    out = {}
    for p in sorted(workdir.rglob("*")):
        rel = p.relative_to(workdir)
        if p.is_file() and not IGNORED_DIRS & set(rel.parts) and rel.name != RESULT_FILE:
            out[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def changed(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}


@dataclass
class TestRun:
    total: int
    passed: int
    failures: list[str] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return self.total - self.passed


def vitest(workdir: Path) -> TestRun:
    report = workdir / RESULT_FILE
    report.unlink(missing_ok=True)
    r = run(["npx", "vitest", "run", "--reporter=json", f"--outputFile={RESULT_FILE}"], cwd=workdir)
    if not report.exists():
        raise RuntimeError(f"vitest wrote no report:\n{tail(r)}")
    data = json.loads(report.read_text())
    failures = []
    for suite in data["testResults"]:
        if suite.get("message") and not suite["assertionResults"]:
            failures.append(f"{Path(suite['name']).name} failed to load: {suite['message'][:1500]}")
        for a in suite["assertionResults"]:
            if a["status"] != "passed":
                msg = " ".join(a.get("failureMessages") or [a["status"]])
                failures.append(f"{a['fullName']}: {msg[:800]}")
    return TestRun(data["numTotalTests"], data["numPassedTests"], failures)


def failure_report(result: TestRun, limit: int = 6000) -> str:
    return "\n".join(result.failures)[:limit]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def build_and_serve(workdir: Path) -> str | None:
    """None if the app builds and the production bundle is served; else what went wrong."""
    r = run(["npm", "run", "build"], cwd=workdir)
    if r.returncode != 0:
        return f"`npm run build` failed:\n{tail(r)}"
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    # Call vite directly, not through npx, so terminate() reaches the server process.
    proc = subprocess.Popen(
        [str(workdir / "node_modules/.bin/vite"), "preview", "--host", "127.0.0.1",
         "--port", str(port), "--strictPort"],
        cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        html = None
        for _ in range(60):
            try:
                html = fetch(base + "/")[1]
                break
            except OSError:
                time.sleep(0.5)
        if html is None:
            return "`vite preview` did not serve / within 30s"
        scripts = re.findall(r'src="(/assets/[^"]+\.js)"', html)
        if 'id="root"' not in html or not scripts:
            return f"the served index.html has no root element or JS bundle:\n{html[:500]}"
        for s in scripts:
            status, _ = fetch(base + s)
            if status != 200:
                return f"bundle {s} returned HTTP {status}"
        return None
    finally:
        proc.terminate()
        proc.wait(timeout=10)


class RunLog:
    """One record per A2A task, written to <workdir>/.factory/run.json."""

    def __init__(self, stages: dict[str, Stage], workdir: Path) -> None:
        self.stages, self.path, self.events = stages, workdir / ".factory" / "run.json", []

    def record(self, stage: str, outcome: str, started: float, detail: str = "") -> None:
        s = self.stages[stage]
        self.events.append({
            "stage": stage, "agent": s.url, "worker": s.worker, "model": s.model,
            "seconds": round(time.monotonic() - started, 1), "outcome": outcome, "detail": detail,
        })  # fmt: skip

    def save(self, status: str) -> None:
        self.path.parent.mkdir(exist_ok=True)
        stages = {k: asdict(v) for k, v in self.stages.items()}
        self.path.write_text(
            json.dumps({"status": status, "stages": stages, "events": self.events}, indent=2) + "\n"
        )


# --- stages -----------------------------------------------------------------


def gather_requirements(agents: Agents, brief: str, workdir: Path, unattended: bool,
                        log: RunLog) -> dict:  # fmt: skip
    rounds = 0

    def on_input(ask: dict) -> dict:
        nonlocal rounds
        rounds += 1
        if rounds > MAX_SPEC_ROUNDS:
            raise RuntimeError(f"no accepted spec after {MAX_SPEC_ROUNDS} rounds")
        spec = ask["spec"]
        step(f"\n  draft spec:\n\n{spec_text(spec)}\n")
        if ask["kind"] == "questions":
            if unattended:
                step("  the analyst has questions; --yes, so it decides them itself")
                return {"answers": ["Decide this yourself and record it under assumptions."] * len(ask["questions"])}
            step("  The analyst has questions:")
            answers = []
            for q in ask["questions"]:
                step(f"  {q}")
                answers.append(input("  > ").strip())
            return {"answers": answers}
        if unattended:
            return {"accept": True}
        choice = input("Accept this spec? [y] accept, [q] quit, or type the changes you want: ").strip()
        if choice.lower() in ("y", "yes"):
            return {"accept": True}
        if choice.lower() in ("q", "quit"):
            raise SystemExit("spec rejected by the user")
        return {"accept": False, "changes": choice}

    started = time.monotonic()
    result = agents.send("requirements", {"brief": brief, "workdir": str(workdir)}, on_input)
    spec = check_spec(result["spec"])
    spec["accepted_by"] = "unattended (--yes)" if unattended else "user"
    log.record("requirements", "accepted", started, f"{rounds} rounds, accepted by {spec['accepted_by']}")
    return spec


def write_tests(agents: Agents, spec: dict, workdir: Path, log: RunLog) -> TestRun:
    feedback = ""
    for attempt in (1, 2):
        started = time.monotonic()
        before = snapshot(workdir)
        agents.send("tests", {"spec": spec, "workdir": str(workdir), "feedback": feedback})
        if bad := sorted(changed(before, snapshot(workdir)) - {TEST_FILE}):
            raise RuntimeError(f"the tests agent modified files outside its role: {', '.join(bad)}")
        result = vitest(workdir)
        if result.total == 0:
            problem = f"the test file does not load:\n{failure_report(result)}"
        elif result.failed == 0:
            problem = "every test passes against an empty app, so no test drives new code"
        else:
            log.record("tests", "red", started, f"{result.total} tests, {result.failed} failing")
            return result
        log.record("tests", "rejected", started, problem[:500])
        step(f"  attempt {attempt} rejected: {problem.splitlines()[0]}")
        feedback = f"Your previous test file was rejected by the orchestrator: {problem}"
    raise RuntimeError("the tests agent did not produce a usable test file")


def implement(agents: Agents, workdir: Path, feedback: str, log: RunLog) -> str | None:
    """Developer round. None if the app builds and serves, else the problem."""
    started = time.monotonic()
    before = snapshot(workdir)
    agents.send("implement", {"workdir": str(workdir), "feedback": feedback})
    touched = changed(before, snapshot(workdir))
    if bad := sorted(p for p in touched if not p.startswith("src/") or p in PROTECTED | {TEST_FILE}):
        log.record("implement", "out of role", started, ", ".join(bad))
        raise RuntimeError(f"the implement agent modified files outside its role: {', '.join(bad)}")
    problem = build_and_serve(workdir)
    log.record("implement", "running" if problem is None else "not running", started,
               f"changed {len(touched)} files" if problem is None else problem[:500])  # fmt: skip
    return problem


def verify(agents: Agents, workdir: Path, log: RunLog) -> tuple[TestRun, dict[str, str]]:
    started = time.monotonic()
    before = snapshot(workdir)
    result = agents.send("verify", {"workdir": str(workdir)})
    if touched := changed(before, snapshot(workdir)):
        raise RuntimeError(f"the verify agent modified files: {', '.join(sorted(touched))}")
    reported = (int(result["passed"]), int(result["failed"]))
    actual = vitest(workdir)
    if reported != (actual.passed, actual.failed):
        raise RuntimeError(
            f"the verify agent reported passed/failed={reported} but vitest says "
            f"{(actual.passed, actual.failed)}"
        )
    log.record("verify", "green" if actual.failed == 0 else "red", started,
               f"passed={actual.passed} failed={actual.failed}")  # fmt: skip
    return actual, result.get("verdicts", {})


# --- driver -----------------------------------------------------------------


def reset_app(workdir: Path) -> None:
    """Remove the previous run's generated files; keep the scaffold."""
    for p in sorted((workdir / "src").rglob("*"), reverse=True):
        rel = p.relative_to(workdir).as_posix()
        if p.is_file() and rel not in PROTECTED:
            p.unlink()
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    (workdir / "src/App.jsx").write_text(APP_SKELETON)
    (workdir / "src/App.css").write_text("")


def open_app(name: str, rebuild: bool) -> tuple[Path, object]:
    """Create apps/<name> from the template if new and lock it. Returns (workdir, lock).

    The lock is held until the returned file object is closed or the process exits, so two
    runs of the same app cannot overlap and a crashed run never leaves a stale lock.
    """
    if not APP_NAME.fullmatch(name):
        raise ConfigError(f"app name {name!r} must be lowercase letters, digits and hyphens")
    workdir = APPS / name
    (workdir / ".factory").mkdir(parents=True, exist_ok=True)
    lock = (workdir / ".factory" / "lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ConfigError(f"app {name!r} is already being built by another run") from None
    if not (workdir / "package.json").exists():
        shutil.copytree(TEMPLATE, workdir, dirs_exist_ok=True)
        step(f"Created apps/{name} from {TEMPLATE.relative_to(ROOT)}")
    elif (workdir / "SPEC.json").exists() and not rebuild:
        lock.close()
        raise ConfigError(
            f"apps/{name} already has a built app; pass --rebuild to overwrite it, "
            "or choose another --app"
        )
    return workdir, lock


def default_app_name(brief: str) -> str:
    if brief == DEFAULT_BRIEF:
        return DEFAULT_APP
    return "-".join(re.findall(r"[a-z0-9]+", brief.lower())[:4])[:40] or "app"


def prepare_workdir(workdir: Path) -> None:
    if not (workdir / "node_modules").exists():
        step("Installing npm dependencies")
        r = run(["npm", "install"], cwd=workdir)
        if r.returncode != 0:
            raise RuntimeError(f"npm install failed:\n{tail(r)}")
    # After npm install, so package-lock.json exists.
    missing = [p for p in PROTECTED if not (workdir / p).exists()]
    if missing:
        raise ConfigError(f"{workdir} is missing scaffold files: {', '.join(sorted(missing))}")


def develop(agents: Agents, spec: dict, workdir: Path, rounds: int, log: RunLog) -> int:
    feedback = ""
    for r in range(1, rounds + 1):
        step(f"\n=== stage 3/4 implement, round {r}/{rounds}: {agents.describe('implement')}")
        problem = implement(agents, workdir, feedback, log)
        if problem:
            step(f"  not running: {problem.splitlines()[0]}")
            feedback = f"Your previous round left the app not running:\n{problem}"
            continue
        step("  builds and serves")

        step(f"\n=== stage 4/4 verify: {agents.describe('verify')}")
        result, verdicts = verify(agents, workdir, log)
        step(f"  passed={result.passed} failed={result.failed} (cross-checked)")
        for c in spec["acceptance_criteria"]:
            step(f"  {c['id']}: {verdicts.get(c['id'], 'no verdict')}")
        if result.failed:
            feedback = f"After your previous round these tests still fail:\n{failure_report(result)}"
            continue
        # Green tests the verifier says miss a criterion are a test gap, not a code bug,
        # so they stop the run instead of going back to the developer.
        gaps = [c["id"] for c in spec["acceptance_criteria"]
                if not verdicts.get(c["id"], "").upper().startswith("PASS")]  # fmt: skip
        if gaps:
            log.save("tests green, criteria not covered")
            step(f"\nTests are green, but the verifier did not accept {', '.join(gaps)}.")
            return 1
        log.save("accepted")
        step(f"\nDone: all {result.total} tests pass and every criterion is accepted.")
        step(f"Run it: cd {workdir.relative_to(ROOT)} && npm run dev")
        return 0
    log.save("failed")
    step(f"\nStopped: not green after {rounds} developer rounds.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brief", default=DEFAULT_BRIEF, help="what to build (stage 1 input)")
    ap.add_argument("--app", help=f"app name, built in apps/<app> (default: {DEFAULT_APP} or from --brief)")
    ap.add_argument("--rebuild", action="store_true", help="overwrite an app that is already built")
    ap.add_argument("--stages", type=Path, default=DEFAULT_STAGES,
                    help="YAML file naming the worker, model and A2A url of each stage")  # fmt: skip
    ap.add_argument("--spec", type=Path, help="reuse an accepted spec and skip stage 1")
    ap.add_argument("--yes", action="store_true", help="accept the spec without asking")
    ap.add_argument("--no-launch", action="store_true", help="connect to agents that are already running")
    args = ap.parse_args()

    cfg = load_config(ROOT / "llm.yaml")
    stages_file = args.stages.resolve()
    stages = load_stages(cfg, stages_file)
    try:
        workdir, lock = open_app(args.app or default_app_name(args.brief), args.rebuild)
    except ConfigError as e:
        sys.exit(f"error: {e}")
    step(f"Building in {workdir.relative_to(ROOT)}")
    prepare_workdir(workdir)
    log = RunLog(stages, workdir)

    with Agents(stages, stages_file, not args.no_launch, workdir / ".factory" / "agents") as agents:
        step("A2A agents:")
        for name in STAGE_NAMES:
            step(f"  {name:<12} {agents.describe(name)}")
        try:
            step(f"\n=== stage 1/4 requirements: {agents.describe('requirements')}")
            if args.spec:
                spec = check_spec(json.loads(args.spec.read_text()))
                step(f"  using accepted spec {args.spec}:\n\n{spec_text(spec)}")
            else:
                spec = gather_requirements(agents, args.brief, workdir, args.yes, log)
                step("  spec accepted")
            reset_app(workdir)
            (workdir / "SPEC.json").write_text(json.dumps(spec, indent=2) + "\n")

            step(f"\n=== stage 2/4 test cases: {agents.describe('tests')}")
            red = write_tests(agents, spec, workdir, log)
            step(f"  {TEST_FILE}: {red.total} tests, RED with {red.failed} failing")

            return develop(agents, spec, workdir, 1 + cfg.budgets.max_retries, log)
        except StageFailed as e:
            log.save("error")
            step(f"\nStopped: an agent failed its task: {e}")
            return 1
        except BaseException:
            log.save("error")
            raise


if __name__ == "__main__":
    sys.exit(main())
