"""Run a plan: send each step to its role's agent, check the result, and keep going or stop.

The engine owns everything that decides whether work is accepted, so no agent grades itself:

- it checks the reply against the step's schema,
- it checks which files the agent changed, and reverts any change outside the agent's role,
- a developer's edit to the tests is kept only if the tester approves it,
- it runs the step's gates (test suites, builds, citations, ...),
- it asks the person at each checkpoint, and records every step in .factory/run.json.

It talks to agents only through `send(role, request, on_input) -> reply`, so the same engine
drives A2A agents (agents.RoleAgents) or fakes in tests.
"""

from __future__ import annotations

import difflib
import fcntl
import json
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import schemas
from .a2a_transport import Json, StageFailed
from .config import Config, ConfigError
from .gates import GATES, Failure, GateCtx, fetch_text
from . import repo
from .paths import ROOT
from .roles import ROLES
from .runbook import LoopDef, Plan, Runbook, StepDef, describe_catalog, fill, holds
from .scaffold import IGNORED, Template, matches, place, reset

Send = Callable[[str, Json, Callable[[Json], Json] | None], Json]
MAX_CONVERSATION = 6  # questions and reviews on one checkpoint before the run stops
API_PREFIX = "/api/"


class Stop(Exception):
    """The run cannot go on."""


class RoundFailed(Exception):
    """A step failed in a way another round may fix. `feedback` maps item key -> message."""

    def __init__(self, feedback: dict[str | None, str]) -> None:
        super().__init__("; ".join(feedback.values()))
        self.feedback = feedback


# --- workspace --------------------------------------------------------------


def open_workspace(apps_dir: Path, name: str, rebuild: bool) -> tuple[Path, Any]:
    """Create and lock apps/<name>. The lock lasts until the file is closed or the process exits."""
    if not schemas.APP_NAME.fullmatch(name):
        raise ConfigError(f"name {name!r} must be lowercase letters, digits and hyphens (at most 40)")
    ws = apps_dir / name
    (ws / ".factory").mkdir(parents=True, exist_ok=True)
    lock = (ws / ".factory" / "lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ConfigError(f"{name!r} is already being worked on by another run") from None
    run_file = ws / ".factory" / "run.json"
    if not rebuild and run_file.exists() and json.loads(run_file.read_text()).get("status") == "accepted":
        lock.close()
        raise ConfigError(f"{apps_dir.name}/{name} already holds accepted work; pass --rebuild to redo it, "
                          "or choose another name")
    return ws, lock


def name_from(brief: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", brief.lower())[:4])[:40].strip("-") or "work"


Snapshot = dict[str, bytes]


def snapshot(root: Path) -> Snapshot:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and not IGNORED & set(rel.parts):
            out[rel.as_posix()] = p.read_bytes()
    return out


def changed(before: Snapshot, after: Snapshot) -> set[str]:
    return {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}


def restore(root: Path, before: Snapshot, paths: set[str]) -> None:
    for rel in paths:
        if rel in before:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(before[rel])
        else:
            (root / rel).unlink(missing_ok=True)


# --- run log ----------------------------------------------------------------


def _empty_usage() -> Json:
    return {"calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0, "calls_without_cost": 0}  # a call with no known cost adds no dollars


def _add_usage(total: Json, call: Json) -> None:
    total["calls"] += 1
    for k in ("input_tokens", "cached_input_tokens", "output_tokens"):
        total[k] += call.get(k) or 0
    if call.get("cost_usd") is None:
        total["calls_without_cost"] += 1
    else:
        total["cost_usd"] = round(total["cost_usd"] + call["cost_usd"], 6)


def new_run_id() -> str:
    """A run ID that sorts by start time: 20261031-142501-a3f9c2."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def factory_version() -> Json:
    """The git commit of the factory code that runs, and whether it has uncommitted changes.
    Both are None when the code is not in a git checkout."""
    def git(*args: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain") if commit else None
    return {"commit": commit, "dirty": None if status is None else bool(status)}


def gate_names(plan: Plan) -> dict[str, list[str]]:
    """The gates of each step and loop of the plan, by id, so a run records which checks accepted it."""
    out: dict[str, list[str]] = {}
    for node in plan.nodes:
        if isinstance(node, LoopDef):
            out[node.loop] = list(node.gates)
            out.update({s.id: list(s.gates) for s in node.steps})
        else:
            out[node.id] = list(node.gates)
    return out


class RunLog:
    def __init__(self, path: Path, plan: Plan, brief: str, run_id: str, target: str | None) -> None:
        self.path, self.lock = path, threading.Lock()
        self.data: Json = {
            "run_id": run_id, "factory": factory_version(), "gates": gate_names(plan),
            "runbook": plan.runbook.name, "brief": brief, "traits": plan.traits, "target": target,
            "surfaces": {d: t.name for d, t in plan.templates.items()},
            "staff": {r: {"worker": s.worker, "model": s.model} for r, s in plan.staff.items()
                      if r in plan.roles()},
            "status": "running", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "usage": {**_empty_usage(), "by_model": {}}, "events": [], "calls": [], "loops": {},
        }

    def resume(self, previous: Json, factory: Json) -> None:
        """Continue the record of an earlier attempt: keep its events, calls and usage."""
        with self.lock:
            self.data = {**previous, "status": "running", "resumes": [
                *previous.get("resumes", []), {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "factory": factory}]}
            self.data.pop("detail", None)
            self.data.pop("ended", None)
            self._write()

    def loop_round(self, loop: str, rounds_used: int, accepted: bool) -> None:
        with self.lock:
            self.data["loops"][loop] = {"rounds_used": rounds_used, "accepted": accepted}
            self._write()

    def record(self, step: str, item: str | None, role: str, status: str, started: float,
               detail: str = "") -> None:
        with self.lock:
            self.data["events"].append({"step": step, "item": item, "role": role, "status": status,
                                        "seconds": round(time.monotonic() - started, 1),
                                        "detail": detail[:1000]})
            self._write()

    def add_usage(self, step: str, item: str | None, role: str, calls: list[Json] | None) -> None:
        """Log the LLM calls of one step: one row each in `calls`, and the sums in `usage`."""
        if not calls:
            return
        with self.lock:
            total = self.data["usage"]
            for c in calls:
                self.data["calls"].append({"step": step, "item": item, "role": role, **c})
                _add_usage(total, c)
                _add_usage(total["by_model"].setdefault(c["model"], _empty_usage()), c)
            self._write()

    def usage_line(self) -> str:
        u = self.data["usage"]
        if not u["calls"]:
            return ""
        cost = f"${u['cost_usd']:.4f}" if u["calls"] > u["calls_without_cost"] else "cost unknown"
        if 0 < u["calls_without_cost"] < u["calls"]:
            cost += f" ({u['calls_without_cost']} of {u['calls']} calls report no cost)"
        return (f"Usage: {u['calls']} LLM calls, {u['input_tokens']:,} input tokens "
                f"({u['cached_input_tokens']:,} cached), {u['output_tokens']:,} output tokens, {cost}")

    def update(self, **fields: Any) -> None:
        with self.lock:
            self.data.update(fields)
            self._write()

    def save(self, status: str, detail: str = "") -> None:
        with self.lock:
            self.data.update(status=status, detail=detail, ended=time.strftime("%Y-%m-%dT%H:%M:%S"))
            self._write()

    def _write(self) -> None:
        self.path.write_text(_dump(self.data))


# --- engine -----------------------------------------------------------------


@dataclass
class Item:
    key: str | None
    data: Json = field(default_factory=dict)
    surface: str | None = None


class Engine:
    def __init__(self, plan: Plan, cfg: Config, workspace: Path, send: Send, *, brief: str,
                 name: str, target: Path | None = None, interactive: bool = True,
                 say: Callable[[str], None] = print, ask: Callable[[str], str] = input,
                 fetch: Callable[[str], str | None] = fetch_text, install: bool = True,
                 earlier_usage: list[Json] | None = None, run_id: str | None = None,
                 resume: bool = False, send_fallback: Send | None = None,
                 worktree: repo.Worktree | None = None, test_command: str | None = None) -> None:
        self.plan, self.cfg, self.ws, self.send = plan, cfg, workspace, send
        self.send_fallback = send_fallback
        self.run_id = run_id or new_run_id()
        self.brief, self.name, self.target = brief, name, target
        self.worktree, self.test_command = worktree, test_command  # P10: a change in a git worktree
        self.interactive, self.say, self.ask, self.fetch, self.install = interactive, say, ask, fetch, install
        self.artifacts: dict[str, Any] = {}
        self._artifacts_lock = threading.Lock()
        self._done: set[tuple[str, str | None]] = set()  # (step, item) accepted in an earlier attempt
        self.log = RunLog(workspace / ".factory" / "run.json", plan, brief, self.run_id,
                          str(target) if target else None)
        self.log.add_usage("triage", None, "triage", earlier_usage)  # made before the engine existed
        self._scaffolds_reset = False
        self._cache: dict[str, str | None] = {}
        self._cache_lock = threading.Lock()
        if plan.runbook.needs_target and (target is None or not target.is_dir()):
            raise ConfigError(f"runbook {plan.runbook.name} needs --target, an existing directory")
        if any(s.writes == "target" for s in plan.steps()) and worktree is None:
            raise ConfigError(f"runbook {plan.runbook.name} writes to a repo, so it needs --repo")
        if worktree is not None:
            self.log.data.update(worktree.fields)  # repo, base, branch, tree, commit (set after commit)
        if resume:
            self._resume()

    def _resume(self) -> None:
        """Load the record of the earlier attempt. Every step item it accepted is kept, not run again."""
        path = self.ws / ".factory" / "run.json"
        if not path.exists():
            raise ConfigError(f"nothing to resume: {path.parent.parent.name} has no run record")
        previous = json.loads(path.read_text())
        if previous.get("runbook") != self.plan.runbook.name:
            raise ConfigError(f"the run record is for runbook {previous.get('runbook')}, not {self.plan.runbook.name}")
        self.run_id = previous["run_id"]
        self.log.resume(previous, factory_version())
        arts = self.ws / ".factory" / "artifacts.json"
        self.artifacts = json.loads(arts.read_text()) if arts.exists() else {}
        latest: dict[tuple[str, str | None], str] = {}
        for e in previous.get("events", []):
            latest[(e["step"], e["item"])] = e["status"]  # the last event of an item decides
        self._done = {key for key, status in latest.items() if status == "accepted"}
        writes = {s.id: s.writes for s in self.plan.steps()}
        # Code that an accepted step wrote must stay: the scaffolds are reset only if none was accepted.
        self._scaffolds_reset = any(writes.get(step, "none") != "none" for step, _ in self._done)

    # --- driver

    def run(self) -> bool:
        """True if the run finished with its deliverable accepted."""
        try:
            for d, tpl in self.plan.templates.items():
                if not (self.ws / d).exists():
                    self.say(f"Placing {tpl.name} in {d}/")
                place(self.ws, d, tpl, install_deps=self.install)
            for node in self.plan.nodes:
                if isinstance(node, LoopDef):
                    self.loop(node)
                else:
                    try:
                        self.step(node, {})  # outside a loop, a round failure ends the run
                    except RoundFailed as e:
                        raise Stop(f"step {node.id} failed:\n" + "\n".join(e.feedback.values())) from None
            if self.worktree is not None:
                self._commit()
        except Stop as e:
            self._save_artifacts()
            self.log.save("stopped", str(e))
            self.say(f"\nStopped: {e}")
            self.say(self.log.usage_line())
            return False
        except BaseException as e:
            self._save_artifacts()
            self.log.save("error", f"{type(e).__name__}: {e}")
            raise
        self._save_artifacts()
        self.log.save("accepted")
        self.say("\nDone.")
        self.say(self.log.usage_line())
        for f in self.plan.runbook.finish:
            if holds(f.when, self.plan.traits):
                self.say(fill(f.text, self._values(None)).strip())
        if self.worktree is not None:
            self._say_repo_result()
        return True

    def _commit(self) -> None:
        """Commit the accepted change on the run's branch (P10). Nothing is pushed or merged."""
        assert self.worktree is not None
        spec = self.artifacts.get("spec") or {}
        title = (spec.get("title") or self.brief).strip().splitlines()[0][:72]
        criteria = [c["text"] for c in spec.get("acceptance_criteria", [])]
        try:
            sha = repo.commit(self.worktree.tree, title, criteria, self.run_id)
        except repo.GitError as e:
            raise Stop(str(e)) from None
        if sha is None:
            raise Stop("the accepted change is empty: no file in the worktree differs from the base, "
                       "so there is nothing to commit")
        self.log.update(commit=sha)
        self.say(f"Committed {sha[:12]} on {self.worktree.branch}: {title}")

    def _say_repo_result(self) -> None:
        wt = self.worktree
        assert wt is not None
        self.say(f"Branch:   {wt.branch} (in {wt.repo})")
        self.say(f"Worktree: {wt.tree}")
        self.say(f"Review:   {repo.diff_command(wt)}")
        self.say(f"Clean up when done: {repo.cleanup_command(wt)}")

    def loop(self, node: LoopDef) -> None:
        if (node.loop, None) in self._done:
            self.say(f"\n=== {node.loop}: accepted in an earlier attempt, kept")
            return
        rounds = 1 + self.cfg.budgets.max_retries if node.rounds == "budget" else node.rounds
        feedback: dict[str | None, str] = {}
        started = time.monotonic()
        for r in range(1, rounds + 1):
            self.say(f"\n=== {node.loop}, round {r}/{rounds}")
            try:
                # After a failed round, redo only the items that failed (when it was per item).
                only = set(feedback) if feedback and None not in feedback else None
                for i, s in enumerate(node.steps):
                    self.step(s, feedback if i == 0 else {}, only if s.for_each == "surfaces" else None)
                ctx = self._ctx(None, self.ws)
                for g in node.gates:
                    GATES[g](ctx, {})
                self.log.loop_round(node.loop, r, True)
                self.log.record(node.loop, None, "loop", "accepted", started, f"round {r}/{rounds}")
                return
            except RoundFailed as e:
                feedback = e.feedback
                for key, msg in feedback.items():
                    self.say(f"  not accepted{f' ({key})' if key else ''}: {msg.splitlines()[0][:200]}")
            except Failure as f:  # a loop gate
                if f.level == "run":
                    raise Stop(f.message) from None
                feedback = {None: f.message}
                self.say(f"  not accepted: {_console_reason(f.message)}")
        problem = "\n".join(feedback.values())[:3000]
        self.log.loop_round(node.loop, rounds, False)
        for s in node.steps:
            self._mark_not_approved(s, rounds, problem)
        raise Stop(f"{node.loop} was not accepted after {rounds} round(s). Last problem:\n{problem}")

    def step(self, s: StepDef, feedback: dict[str | None, str], only: set[str] | None = None) -> None:
        items = [i for i in self._items(s) if only is None or i.key in only]
        staff = self.plan.staff[s.role]
        label = f" for {', '.join(str(i.key) for i in items)}" if s.for_each else ""
        self.say(f"\n--- {s.id}{label}: {ROLES[s.role].title} ({staff.worker} / {staff.model})")
        failures: dict[str | None, str] = {}
        self._reset_scaffolds(s)

        def one(item: Item) -> None:
            if (s.id, item.key) in self._done and self._stored(s, item):
                self.say(f"  {s.id}{f' [{item.key}]' if item.key else ''}: accepted in an earlier attempt, kept")
                return
            fb = feedback.get(item.key) or feedback.get(None, "")
            try:
                out = self.attempt(s, item, fb)
            except RoundFailed as e:
                failures.update(e.feedback)
                return
            self._store(s, item, out)

        if s.parallel and len(items) > 1:
            with ThreadPoolExecutor(min(len(items), 6)) as pool:
                list(pool.map(one, items))
        else:
            for item in items:
                one(item)
        if failures:
            raise RoundFailed(failures)
        if s.save:
            self._save(s)

    def attempt(self, s: StepDef, item: Item, feedback: str) -> Json:
        """Run one item of a step, retrying it with feedback. Raises RoundFailed or Stop."""
        cwd = self._cwd(s, item)
        in_tree = self._in_tree(cwd)  # a repo worktree: git tells what changed, not a folder snapshot
        tag = f"{s.id}{f' [{item.key}]' if item.key else ''}"
        staff = self.plan.staff[s.role]
        for attempt in range(1 + s.retries):
            started = time.monotonic()
            before = repo.snapshot_tree(cwd) if in_tree else snapshot(cwd)
            top_before = repo.dirty_files(self.worktree.repo) if in_tree else {}
            # On the last attempt, a role with a fallback uses its fallback worker (FR-7).
            # The last retry, so a step with no retries never switches.
            switch = (staff.fallback is not None and self.send_fallback is not None
                      and s.retries > 0 and attempt == s.retries)
            if switch:
                self.say(f"  {tag}: last attempt on the fallback worker "
                         f"({staff.fallback.worker} / {staff.fallback.model})")
            try:
                reply = self._send(s.id, item.key, s.role, self._request(s, item, cwd, feedback),
                                   self._on_input(s), via=self.send_fallback if switch else None)
                out = reply.get("output", {})
                if in_tree:  # checked before the reply is read, so a bad reply cannot hide a change
                    self._check_tree(s, item, cwd, before, top_before)
                if s.output:
                    out = schemas.validate(s.output, out, self._schema_ctx(s))
                if not in_tree:
                    self._check_files(s, item, cwd, before, out)
                ctx = self._ctx(item, cwd)
                for g in s.gates:
                    GATES[g](ctx, out)
            except StageFailed as e:
                problem, level = f"the agent could not finish the task: {e}", "step"
            except ValueError as e:  # the reply does not match the schema
                problem, level = f"your reply was not usable: {e}", "step"
            except Failure as f:
                problem, level = f.message, f.level
            else:
                self.log.record(s.id, item.key, s.role, "accepted", started, _summary(s, out))
                self.say(f"  {tag}: accepted{_summary_suffix(s, out)}")
                return out
            self.log.record(s.id, item.key, s.role, "rejected", started, problem)
            self.say(f"  {tag}: not accepted: {_console_reason(problem)}")
            if level == "run":
                raise Stop(problem)
            if level == "round" or attempt == s.retries:
                raise RoundFailed({item.key: problem})
            feedback = problem
        raise AssertionError("unreachable")

    def _send(self, step: str, item: str | None, role: str, request: Json,
              on_input: Callable[[Json], Json] | None, via: Send | None = None) -> Json:
        """Send a request to an agent and log the LLM calls it made, also when it fails.
        `via` sends it to another agent of the role (the fallback)."""
        try:
            reply = (via or self.send)(role, request, on_input)
        except StageFailed as e:
            self.log.add_usage(step, item, role, e.data.get("usage"))
            raise
        self.log.add_usage(step, item, role, reply.get("usage"))
        return reply

    # --- requests

    def _request(self, s: StepDef, item: Item, cwd: Path, feedback: str) -> Json:
        tpl = self.plan.templates.get(item.surface) if item.surface else None
        stack = ""
        if tpl:
            stack = "\n".join(x for x in (tpl.stack, self.plan.notes.get(item.surface, "")) if x)
        context = [c for r in s.reads if (c := self._context(r)) is not None]
        if s.writes == "none" and self._in_tree(cwd) and repo.status_paths(cwd):
            # A checking step in a repo reads the change, as git shows it (P10).
            context.append({"title": "The change so far (git diff against the base)",
                            "body": f"```diff\n{repo.diff_text(cwd)}\n```"})
        return {
            "run_id": self.run_id, "task": s.id, "item": item.key,
            "instructions": fill(s.instructions, self._values(item)),
            "stack": stack,
            "context": context,
            "output": s.output, "schema_ctx": self._schema_ctx(s),
            "commands": self._commands(item),
            "cwd": str(cwd), "feedback": feedback,
            "interactive": self.interactive, "checkpoint": self.interactive and s.checkpoint,
        }

    def _commands(self, item: Item | None) -> dict[str, str]:
        """The commands an agent may run: its surface's template, or the repo's test command (P10)."""
        tpl = self.plan.templates.get(item.surface) if item and item.surface else None
        if tpl:
            return dict(tpl.commands)
        return {"test": self.test_command} if self.test_command else {}

    def _values(self, item: Item | None) -> Json:
        tpl = self.plan.templates.get(item.surface) if item and item.surface else None
        commands = self._commands(item)
        return {
            "brief": self.brief, "name": self.name, "workspace": str(self.ws),
            "target": str(self.target) if self.target else "",
            "traits": self.plan.traits, "surfaces": self.plan.surfaces,
            "item": item.data if item else {},
            "test_files": list(tpl.test_files) if tpl else [],
            "source_files": list(tpl.source_files) if tpl else [],
            "test_command": commands.get("test", ""),
            "check_commands": " and ".join(f"`{c}`" for c in commands.values()),
        }

    def _context(self, ref: str) -> Json | None:
        if ref not in self.artifacts:
            return None  # its step did not run for these traits
        data, out_name = self.artifacts[ref], self._output_of(ref)
        if self._per_item(ref):
            body = "\n\n".join(f"[{k}]\n{schemas.render(out_name, v)}" for k, v in data.items())
        else:
            body = schemas.render(out_name, data)
        return {"title": f"{ref} (from an earlier step)", "body": body}

    def _schema_ctx(self, s: StepDef) -> Json:
        if s.output == "spec":
            return {"surfaces": self.plan.surfaces}
        if s.output == "api_contract" and {"web", "api"} <= set(self.plan.surfaces):
            return {"api_prefix": API_PREFIX}  # the web dev server proxies /api to the backend
        return {}

    def _on_input(self, s: StepDef) -> Callable[[Json], Json] | None:
        if not self.interactive or s.parallel:
            return None
        turns = 0

        def answer(data: Json) -> Json:
            nonlocal turns
            turns += 1
            if turns > MAX_CONVERSATION:
                raise Stop(f"no agreement on {s.id} after {MAX_CONVERSATION} rounds of questions and review")
            if data.get("kind") == "questions":
                self.say("\nThe agent has questions (press Enter to let it decide):")
                return {"answers": [self.ask(f"  {q}\n  > ").strip() or "You decide." for q in data["questions"]]}
            self.say(f"\n{data.get('rendered', '')}\n")
            reply = self.ask("Accept? Press Enter to accept, or type the changes you want: ").strip()
            if reply.lower() in ("", "y", "yes"):
                return {"accept": True}
            return {"accept": False, "changes": reply}

        return answer

    # --- checks

    def _check_files(self, s: StepDef, item: Item, cwd: Path, before: Snapshot, out: Json) -> None:
        touched = changed(before, snapshot(cwd))
        if not touched:
            return
        if s.writes == "none":
            # Not fatal: research and review agents sometimes leave scratch files behind.
            restore(cwd, before, touched)
            self.say(f"  reverted files the {s.role} may not change: {', '.join(sorted(touched))[:300]}")
            self.log.record(s.id, item.key, s.role, "files reverted", time.monotonic(), ", ".join(sorted(touched)))
            return
        tpl: Template = self.plan.templates[item.surface]
        allowed = tpl.test_files if s.writes == "tests" else tpl.source_files + tpl.test_files
        bad = {p for p in touched if p in tpl.protected or not matches(p, allowed)}
        if bad:
            restore(cwd, before, bad)
            raise Failure(f"you changed files outside your role: {', '.join(sorted(bad))}. They were "
                          f"restored. You may change only {', '.join(allowed)}, never "
                          f"{', '.join(tpl.protected)}.")
        if s.writes == "source":
            if tests := {p for p in touched if matches(p, tpl.test_files)}:
                self._review_test_change(s, item, cwd, before, tests, out.get("test_change", ""))

    def _check_tree(self, s: StepDef, item: Item, cwd: Path, before: repo.TreeState, top_before: dict) -> None:
        """A step in the repo worktree (P10). Nothing outside the worktree may change, and git's own
        data (.git, HEAD) must not change. Files in the worktree may change only when the step
        writes target; a step that writes nothing has its scratch files reverted."""
        assert self.worktree is not None
        problems = []
        top = self.worktree.repo
        if outside := repo.touched(top_before, repo.dirty_files(top)):
            repo.revert_files(top, top_before, outside)
            problems.append("you changed files outside your worktree, in the original checkout: "
                            f"{', '.join(sorted(outside))}. They were restored. Change files only in "
                            "your working directory.")
        if found := repo.revert_git(cwd, before):
            problems.append(f"you changed git's own data ({', '.join(found)}). It was restored. Do not "
                            "commit or move HEAD: the factory makes the commit.")
        if s.writes == "none":
            if touched := repo.touched(before.files, repo.dirty_files(cwd)):
                repo.revert_files(cwd, before.files, touched)
                self.say(f"  reverted files the {s.role} may not change: {', '.join(sorted(touched))[:300]}")
                self.log.record(s.id, item.key, s.role, "files reverted", time.monotonic(), ", ".join(sorted(touched)))
        if problems:
            raise Failure("\n".join(problems), "step")

    def _in_tree(self, cwd: Path) -> bool:
        return self.worktree is not None and cwd.resolve() == self.worktree.tree

    def _review_test_change(self, s: StepDef, item: Item, cwd: Path, before: Snapshot,
                            tests: set[str], reason: str) -> None:
        """The tester approves a developer's change to the tests, or it is reverted."""
        started = time.monotonic()
        diff = "".join(
            line for p in sorted(tests) for line in difflib.unified_diff(
                before.get(p, b"").decode(errors="replace").splitlines(keepends=True),
                (cwd / p).read_text(errors="replace").splitlines(keepends=True) if (cwd / p).exists() else [],
                f"before/{p}", f"after/{p}")
        )
        if not reason:
            verdict = {"verdict": "revise", "issues": ["the tests were changed without a test_change reason"]}
        else:
            spec = self.artifacts.get("spec")
            req = {
                "run_id": self.run_id, "task": f"{s.id}:test-review", "item": item.key,
                "output": "verdict", "cwd": str(cwd),
                "stack": self.plan.templates[item.surface].stack, "commands": {},
                "instructions": (
                    "The developer changed the tests you wrote, and gives a reason. Approve only if the "
                    "old test was wrong and the new one checks the same criterion as strictly, for "
                    "example a query that matched the wrong element or an expectation that contradicts "
                    "the spec. Answer revise if the change weakens, removes or skips a check, drops the "
                    "only test for a criterion, or changes what the spec requires. Put the reason in "
                    "issues."),
                "context": ([{"title": "Accepted spec", "body": schemas.render("spec", spec)}] if spec else [])
                + [{"title": "The developer's reason", "body": reason},
                   {"title": "Diff of the tests", "body": f"```diff\n{diff[:12000]}```"}],
            }
            try:
                reply = self._send(f"{s.id}:test-review", item.key, "tester", req, None)
                verdict = schemas.validate("verdict", reply.get("output", {}))
            except (StageFailed, ValueError) as e:
                verdict = {"verdict": "revise", "issues": [f"the tester could not review the change: {e}"]}
        why = "; ".join(verdict.get("issues", [])) or "no reason given"
        if verdict["verdict"] == "approve":
            self.log.record(s.id, item.key, "tester", "test change approved", started, reason)
            self.say(f"  tester approved a test change: {reason[:200]}")
            return
        restore(cwd, before, tests)
        self.log.record(s.id, item.key, "tester", "test change rejected", started, why)
        raise Failure(f"the tester rejected your change to {', '.join(sorted(tests))}: {why}. "
                      "The tests were restored; make the code pass them as they are.")

    def _ctx(self, item: Item | None, cwd: Path) -> GateCtx:
        dirs = [item.surface] if item and item.surface else self.plan.surfaces
        return GateCtx(app_dir=self.ws, cwd=cwd, dirs=dirs, templates=self.plan.templates,
                       artifacts=self.artifacts, fetch=self._fetch,
                       tree=self.worktree.tree if self.worktree else None, test_command=self.test_command)

    def _fetch(self, url: str) -> str | None:
        with self._cache_lock:
            if url in self._cache:
                return self._cache[url]
        text = self.fetch(url)
        with self._cache_lock:
            self._cache[url] = text
        return text

    # --- items, directories, artifacts

    def _items(self, s: StepDef) -> list[Item]:
        fe = s.for_each
        if fe is None:
            return [Item(None)]
        if fe == "surfaces":
            return [Item(d, {"surface": d, "template": t.name, "kind": t.surface}, d)
                    for d, t in self.plan.templates.items()]
        if isinstance(fe, list):
            return [Item(str(x), {"name": x}) for x in fe]
        src, attr = fe.split(".", 1)
        values = (self.artifacts.get(src) or {}).get(attr) or []
        if not values:
            raise Stop(f"step {s.id}: {fe} is empty, so there is nothing to do for each of it")
        return [Item(str(v.get("id", i + 1)) if isinstance(v, dict) else str(v),
                     v if isinstance(v, dict) else {"name": v}) for i, v in enumerate(values)]

    def _reset_scaffolds(self, s: StepDef) -> None:
        """The first step that writes code starts from clean scaffolds: the build comes from the
        accepted spec alone. Earlier steps may still read what was there before. Called before
        any of the step's items start, so parallel items never race a reset."""
        if s.writes != "none" and not self._scaffolds_reset:
            for d, tpl in self.plan.templates.items():
                reset(self.ws, d, tpl)
            self._scaffolds_reset = True

    def _cwd(self, s: StepDef, item: Item) -> Path:
        if s.cwd == "surface":
            return self.ws / item.surface
        if s.cwd == "target":
            return self.target
        return self.ws

    def _stored(self, s: StepDef, item: Item) -> bool:
        if s.for_each:
            return item.key in (self.artifacts.get(s.id) or {})
        return s.id in self.artifacts

    def _store(self, s: StepDef, item: Item, out: Json) -> None:
        with self._artifacts_lock:
            if s.for_each:
                self.artifacts.setdefault(s.id, {})[item.key] = out
            else:
                self.artifacts[s.id] = out
            if s.output and s.output not in self._step_ids():
                self.artifacts[s.output] = self.artifacts[s.id]
            self._save_artifacts()  # so a run that stops or crashes can be resumed

    def _save(self, s: StepDef) -> None:
        path = self.ws / fill(s.save, self._values(None))
        data = self.artifacts[s.id]
        if s.output == "report":
            path.write_text(data["markdown"].rstrip() + "\n")
        else:
            path.write_text(_dump(data))
        self.say(f"  saved {path.relative_to(self.ws)}")

    def _mark_not_approved(self, s: StepDef, rounds: int, problem: str) -> None:
        """Put a banner on a saved Markdown draft that its loop never got accepted."""
        if not (s.save and s.output == "report"):
            return
        path = self.ws / fill(s.save, self._values(None))
        if not path.exists():
            return
        quoted = "\n".join(f"> {line}" if line else ">" for line in problem[:1500].splitlines())
        path.write_text(f"> **NOT APPROVED.** This is a draft. It was not accepted after {rounds} round(s).\n"
                        f">\n{quoted}\n\n" + path.read_text())
        self.say(f"  marked {path.relative_to(self.ws)} as not approved")

    def _save_artifacts(self) -> None:
        """Write the artifacts. The caller holds _artifacts_lock, or runs alone."""
        (self.ws / ".factory" / "artifacts.json").write_text(_dump(self.artifacts))

    def _step_ids(self) -> set[str]:
        return {s.id for s in self.plan.steps()}

    def _output_of(self, ref: str) -> str | None:
        for s in self.plan.steps():
            if s.id == ref:
                return s.output
        return ref if ref in schemas.SCHEMAS else None

    def _per_item(self, ref: str) -> bool:
        for s in self.plan.steps():
            if s.id == ref or s.output == ref:
                return bool(s.for_each)
        return False


def _dump(data: Json) -> str:
    return json.dumps(data, indent=2) + "\n"


def _summary(s: StepDef, out: Json) -> str:
    if s.output == "findings":
        return f"{len(out['findings'])} verified, {len(out.get('rejected', []))} rejected"
    if s.output == "audit_findings":
        return f"{len(out['findings'])} findings, {len(out.get('rejected', []))} rejected"
    if s.output == "verification":
        return f"passed={out['passed']} failed={out['failed']}"
    return str(out.get("summary") or out.get("title") or "")[:300]



def _console_reason(msg: str, lines: int = 12, width: int = 300) -> str:
    """A rejection reason for the console: the first line, then the bullet lines indented, capped."""
    rows = [r[:width] for r in msg.splitlines() if r.strip()]
    shown = rows[:lines]
    if len(rows) > lines:
        shown.append(f"... ({len(rows) - lines} more lines in .factory/run.json)")
    return "\n    ".join(shown)

def _summary_suffix(s: StepDef, out: Json) -> str:
    """The short result shown after "accepted" on the console, for the steps that report counts."""
    summary = _summary(s, out)
    return f" ({summary.splitlines()[0][:120]})" if summary and s.output in (
        "findings", "audit_findings", "verification") else ""


# --- triage -----------------------------------------------------------------


def triage(send: Send, brief: str, books: dict[str, Runbook], *, target: str | None,
           on_input: Callable[[Json], Json] | None, cwd: Path,
           usage: list[Json] | None = None, run_id: str | None = None) -> Json:
    """Ask the triage agent which runbook and traits fit the brief.

    The LLM calls it makes are added to `usage`, for the run log."""
    req = {
        "run_id": run_id, "task": "triage", "item": None, "output": "triage", "cwd": str(cwd), "commands": {},
        "instructions": (
            "Read the request and choose the runbook that fits it, and its traits. Also choose a short "
            "name for the work: lowercase words joined by hyphens. If the request names existing code "
            "to work on, put its path in target."),
        "context": [{"title": "The request", "body": brief},
                    {"title": "Runbooks", "body": describe_catalog(books)}]
        + ([{"title": "Target given by the user", "body": target}] if target else []),
        "schema_ctx": {"runbooks": {n: list(rb.traits) for n, rb in books.items()}},
        "interactive": False, "checkpoint": on_input is not None, "feedback": "",
    }
    try:
        reply = send("triage", req, on_input)
    except StageFailed as e:
        if usage is not None:
            usage.extend(e.data.get("usage") or [])
        raise
    if usage is not None:
        usage.extend(reply.get("usage") or [])
    return schemas.validate("triage", reply.get("output", {}), req["schema_ctx"])
