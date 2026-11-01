"""The run task list (P20): one entry per step and item, with the item's sub-tasks.

A stopped run (tokens used up, the LLM or the network down) continues from the last done sub-task.
The engine writes the list when the run starts, and keeps it current:

    .factory/tasks.json   the list, read back by --resume
    .factory/TASKS.md     the same list, for a person

An entry is one step and one item: `todo`, `running`, `accepted` or `failed`. A sub-task is one
acceptance criterion of the spec, for a step that reads the spec and writes code (the tests, the
build, the change, the fix). The agent marks a criterion done in its reply (`criteria_done`). The
engine keeps that mark only when the work stays in the folder: an attempt whose files were reverted
marks nothing. On --resume the request lists the open criteria to do, and the done ones whose work
is already in the folder. An accepted item is done as a whole and is not sent again.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .runbook import LoopDef, Plan, StepDef

Json = dict[str, Any]
STATES = ("todo", "running", "accepted", "failed")


def has_subtasks(s: StepDef) -> bool:
    """A step whose work an acceptance criterion can split: it reads the spec and writes code."""
    return "spec" in s.reads and s.writes != "none"


def key(step: str, item: str | None) -> str:
    return f"{step}[{item}]" if item else step


class TaskList:
    def __init__(self, folder: Path) -> None:
        self.json_path, self.md_path = folder / "tasks.json", folder / "TASKS.md"
        self.lock = threading.Lock()
        self.entries: dict[str, Json] = {}

    # --- building and loading

    def plan(self, plan: Plan) -> None:
        """One `todo` entry for each step of the plan. Items are added when a step finds them."""
        with self.lock:
            for node in plan.nodes:
                steps = node.steps if isinstance(node, LoopDef) else [node]
                loop = node.loop if isinstance(node, LoopDef) else None
                for s in steps:
                    if not s.for_each:
                        self.entries.setdefault(key(s.id, None), _entry(s, None, loop))
            self._write()

    def load(self, done: set[tuple[str, str | None]]) -> None:
        """Read the list of an earlier attempt. Entries the run record accepted are `accepted`;
        any other entry that was `running` or `failed` goes back to `todo`, with its done sub-tasks kept."""
        if not self.json_path.exists():
            return
        with self.lock:
            for e in json.loads(self.json_path.read_text()).get("tasks", []):
                k = key(e["step"], e["item"])
                if (e["step"], e["item"]) in done:
                    e["status"] = "accepted"
                elif e["status"] in ("running", "failed"):
                    e["status"] = "todo"  # it runs again; the sub-tasks it finished stay done
                self.entries[k] = {**self.entries.get(k, {}), **e}
            self._write()

    def items(self, s: StepDef, loop: str | None, items: list[str | None], criteria: list[Json]) -> None:
        """Add the items a step works on, and the sub-tasks of each (kept if already there)."""
        with self.lock:
            if any(i is not None for i in items):
                self.entries.pop(key(s.id, None), None)  # the step entry is replaced by its items
            for item in items:
                e = self.entries.setdefault(key(s.id, item), _entry(s, item, loop))
                if has_subtasks(s):
                    have = {t["id"] for t in e["subtasks"]}
                    e["subtasks"] += [{"id": c["id"], "text": c["text"], "done": False}
                                      for c in _criteria_for(criteria, item) if c["id"] not in have]
            self._write()

    # --- state changes

    def set(self, step: str, item: str | None, status: str, *, settled: bool = True) -> None:
        """`settled` is false for an accepted step inside a loop: the loop gates can still reject
        the round, so its sub-tasks are done only when the loop is accepted (`settle`)."""
        assert status in STATES, status
        with self.lock:
            if e := self.entries.get(key(step, item)):
                e["status"] = status
                if status == "accepted" and settled:
                    for t in e["subtasks"]:
                        t["done"] = True
                self._write()

    def settle(self, loop: str) -> None:
        """The loop was accepted: every sub-task of its accepted entries is done."""
        with self.lock:
            for e in self.entries.values():
                if e.get("loop") == loop and e["status"] == "accepted":
                    for t in e["subtasks"]:
                        t["done"] = True
            self._write()

    def mark_done(self, step: str, item: str | None, ids: list[str]) -> list[str]:
        """Mark sub-tasks done. Unknown ids are passed over. Returns the ids that changed."""
        changed = []
        with self.lock:
            e = self.entries.get(key(step, item))
            if not e:
                return changed
            for t in e["subtasks"]:
                if t["id"] in ids and not t["done"]:
                    t["done"] = True
                    changed.append(t["id"])
            if changed:
                self._write()
        return changed

    def reset_subtasks(self, step: str, item: str | None) -> None:
        """The item's work was thrown away (a scaffold reset): its sub-tasks are open again."""
        with self.lock:
            if e := self.entries.get(key(step, item)):
                for t in e["subtasks"]:
                    t["done"] = False
                self._write()

    # --- reading

    def status(self, step: str, item: str | None) -> str | None:
        with self.lock:
            e = self.entries.get(key(step, item))
            return e["status"] if e else None

    def subtasks(self, step: str, item: str | None) -> list[Json]:
        with self.lock:
            e = self.entries.get(key(step, item))
            return [dict(t) for t in e["subtasks"]] if e else []

    def as_json(self) -> Json:
        with self.lock:
            return {"tasks": [dict(e, subtasks=[dict(t) for t in e["subtasks"]]) for e in self.entries.values()]}

    def text(self) -> str:
        """The list as a person reads it: one line per entry, then its sub-tasks."""
        with self.lock:
            return _markdown(list(self.entries.values()))

    def _write(self) -> None:
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        entries = list(self.entries.values())
        self.json_path.write_text(json.dumps({"tasks": entries}, indent=2) + "\n")
        self.md_path.write_text("# Run task list\n\n" + _markdown(entries) + "\n")


def _entry(s: StepDef, item: str | None, loop: str | None) -> Json:
    return {"step": s.id, "item": item, "role": s.role, "loop": loop, "status": "todo", "subtasks": []}


def _criteria_for(criteria: list[Json], item: str | None) -> list[Json]:
    """The criteria of one item: a surface item gets the criteria of its surface, others get all."""
    if item is None:
        return criteria
    mine = [c for c in criteria if c.get("surface") == item]
    return mine if any(c.get("surface") for c in criteria) else criteria


MARK = {"todo": "[ ]", "running": "[~]", "accepted": "[x]", "failed": "[!]"}


def _markdown(entries: list[Json]) -> str:
    lines = []
    for e in entries:
        where = f" ({e['loop']})" if e.get("loop") else ""
        lines.append(f"- {MARK[e['status']]} {key(e['step'], e['item'])}{where}: {e['role']}, {e['status']}")
        lines += [f"  - [{'x' if t['done'] else ' '}] {t['id']}: {t['text']}" for t in e["subtasks"]]
    return "\n".join(lines) or "(no tasks)"


def plan_text(plan: Plan) -> str:
    """The task list a run of the plan starts with, before any item is known: for `factory plan`."""
    lines = []
    for node in plan.nodes:
        steps = node.steps if isinstance(node, LoopDef) else [node]
        loop = f" ({node.loop})" if isinstance(node, LoopDef) else ""
        for s in steps:
            each = (f", one per item of {s.for_each}" if isinstance(s.for_each, str)
                    else f", one per {', '.join(map(str, s.for_each))}" if s.for_each else "")
            sub = "; one sub-task per acceptance criterion" if has_subtasks(s) else ""
            lines.append(f"- [ ] {s.id}{each}{loop}: {s.role}{sub}")
    return "\n".join(lines)
