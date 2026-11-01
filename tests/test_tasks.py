"""The run task list (P20): tasks.json and TASKS.md, sub-tasks from the spec, and resume (no LLM).

A sub-task is one acceptance criterion, for a step that reads the spec and writes code. The agent
marks the criteria its work meets in `criteria_done`. A resume sends only the open ones.
"""

import json
import sys

import pytest

from factory import repo
from factory.engine import Engine, open_workspace
from factory.runbook import catalog, resolve
from factory.tasks import TaskList, has_subtasks, plan_text
from test_engine import Agents, strip_html, write_files
from test_repo import APPROVE, SPEC, change_plan, git, repo_dir, run_record, scratch  # noqa: F401
from test_usage import cfg, staff  # noqa: F401  (fixtures)

WRITES = {"app.py": "def cart():\n    return ['a']\n"}


def tasks_of(ws):
    return json.loads((ws / ".factory" / "tasks.json").read_text())["tasks"]


def entry(ws, step, item=None):
    return next(e for e in tasks_of(ws) if e["step"] == step and e["item"] == item)


def engine_for(ws, wt, cfg, staff, agents, *, resume=False, said=None, test_command="true"):
    return Engine(change_plan(cfg, staff), cfg, ws, agents, brief="add a cart", name=ws.name,
                  target=wt.tree, say=(said.append if said is not None else lambda m: None),
                  fetch=lambda url: strip_html(""), install=False, worktree=wt, test_command=test_command,
                  resume=resume)


def first_run(repo_dir, scratch, cfg, staff, developer, said=None, test_command="true"):
    ws, lock = open_workspace(scratch / "shop", "cart", rebuild=False)
    wt = repo.open_worktree(repo_dir, "cart", None, ws)
    agents = Agents({("analyst", "spec"): [SPEC], ("developer", "change"): developer,
                     ("reviewer", "verify"): APPROVE})
    engine = engine_for(ws, wt, cfg, staff, agents, said=said, test_command=test_command)
    return engine, ws, wt, lock, agents


class Down(Exception):
    """The LLM gateway or the network went away."""


# --- the list --------------------------------------------------------------------------------


def test_the_list_is_written_when_the_run_starts(repo_dir, scratch, cfg, staff):
    engine, ws, wt, lock, _ = first_run(repo_dir, scratch, cfg, staff, lambda r: {"summary": "x"})
    lock.close()
    assert [(e["step"], e["status"]) for e in tasks_of(ws)] == [("spec", "todo"), ("change", "todo"),
                                                                 ("verify", "todo")]
    assert (ws / ".factory" / "TASKS.md").read_text().startswith("# Run task list")


def test_an_accepted_run_marks_every_entry_and_sub_task_done(repo_dir, scratch, cfg, staff):
    def developer(req):
        write_files(req["cwd"], WRITES)
        return {"summary": "done", "criteria_done": ["AC1", "AC2"]}

    engine, ws, wt, lock, _ = first_run(repo_dir, scratch, cfg, staff, developer)
    try:
        assert engine.run()
    finally:
        lock.close()
    assert {e["status"] for e in tasks_of(ws)} == {"accepted"}
    change = entry(ws, "change")
    assert [(t["id"], t["done"]) for t in change["subtasks"]] == [("AC1", True), ("AC2", True)]
    text = (ws / ".factory" / "TASKS.md").read_text()
    assert "- [x] change (build): developer, accepted" in text and "  - [x] AC2: The cart total is shown" in text


def test_the_request_carries_the_open_sub_tasks(repo_dir, scratch, cfg, staff):
    def developer(req):
        write_files(req["cwd"], WRITES)
        return {"summary": "done"}

    engine, ws, wt, lock, agents = first_run(repo_dir, scratch, cfg, staff, developer)
    try:
        engine.run()
    finally:
        lock.close()
    req = next(r for role, r in agents.requests if role == "developer")
    assert [t["id"] for t in req["subtasks"]] == ["AC1", "AC2"] and req["subtasks_done"] == []
    spec_req = next(r for role, r in agents.requests if role == "analyst")
    assert spec_req["subtasks"] == []  # the spec step has no sub-tasks: it writes the criteria


def test_the_run_start_prints_the_task_list(repo_dir, scratch, cfg, staff):
    said = []
    engine, ws, wt, lock, _ = first_run(repo_dir, scratch, cfg, staff, lambda r: {"summary": "x"}, said=said)
    try:
        engine.run()
    finally:
        lock.close()
    assert said[0].startswith("\nTask list (.factory/TASKS.md):") and "- [ ] change (build)" in said[0]


# --- stop mid-item and resume -----------------------------------------------------------------


def test_a_stop_mid_item_keeps_the_done_sub_tasks_and_resume_sends_only_the_open_ones(repo_dir, scratch,
                                                                                    cfg, staff):
    attempts = {"n": 0}

    def half_done(req):  # round 1 builds AC1, the repo tests fail; round 2 hits a dead network
        attempts["n"] += 1
        if attempts["n"] > 1:
            raise Down("connection reset by the gateway")
        write_files(req["cwd"], WRITES)
        return {"summary": "AC1 only", "criteria_done": ["AC1"]}

    failing = f'{sys.executable} -c "import sys; sys.exit(1)"'
    engine, ws, wt, lock, _ = first_run(repo_dir, scratch, cfg, staff, half_done, test_command=failing)
    try:
        with pytest.raises(Down):
            engine.run()
    finally:
        lock.close()
    change = entry(ws, "change")
    assert [(t["id"], t["done"]) for t in change["subtasks"]] == [("AC1", True), ("AC2", False)]
    assert change["status"] == "failed" and run_record(ws)["status"] == "error"
    assert attempts["n"] == 2  # the first round was rejected by the tests, the second never came back

    # Resume: the spec is kept, the developer is sent AC2 only, and told AC1 is already in the folder.
    ws, lock = open_workspace(scratch / "shop", "cart", rebuild=True)
    seen = []

    def developer(req):
        seen.append(req)
        write_files(req["cwd"], {"app.py": "def cart():\n    return ['a']\n\n\ndef total():\n    return 1\n"})
        return {"summary": "AC2", "criteria_done": ["AC2"]}

    agents = Agents({("developer", "change"): developer, ("reviewer", "verify"): APPROVE})
    resumed = engine_for(ws, repo.reuse_worktree(run_record(ws)), cfg, staff, agents, resume=True)
    try:
        assert resumed.run()
    finally:
        lock.close()
    assert "analyst" not in {role for role, _ in agents.requests}
    req = seen[0]
    assert [t["id"] for t in req["subtasks"]] == ["AC2"] and req["subtasks_done"] == ["AC1"]
    body = next(c["body"] for c in req["context"] if c["title"].startswith("Sub-tasks"))
    assert "Already done" in body and "AC1: Adding an item shows it in the cart" in body
    assert body.index("AC1") < body.index("Still to do") < body.index("AC2")
    assert {e["status"] for e in tasks_of(ws)} == {"accepted"}


def test_a_crash_prints_the_resume_command(repo_dir, scratch, cfg, staff):
    def crash(req):
        raise Down("the LLM gateway is down")

    said = []
    engine, ws, wt, lock, _ = first_run(repo_dir, scratch, cfg, staff, crash, said=said)
    try:
        with pytest.raises(Down):
            engine.run()
    finally:
        lock.close()
    text = "\n".join(said)
    assert "Stopped by an error: Down: the LLM gateway is down" in text
    assert "factory run --resume --name cart --repo" in text and "--test-command true" in text
    assert entry(ws, "change")["status"] == "failed"  # a resume puts it back to todo and runs it again


def test_a_stopped_run_prints_the_resume_command(repo_dir, scratch, cfg, staff):
    said = []
    engine, ws, wt, lock, _ = first_run(repo_dir, scratch, cfg, staff, lambda r: {"summary": "nothing"},
                                        said=said)
    try:
        assert not engine.run()  # the change is empty: the run stops before the commit
    finally:
        lock.close()
    assert any(m.startswith("Continue it with: factory run --resume --name cart") for m in said)


def test_a_resume_on_an_accepted_item_sends_nothing_for_it(repo_dir, scratch, cfg, staff):
    def developer(req):
        write_files(req["cwd"], WRITES)
        return {"summary": "done", "criteria_done": ["AC1", "AC2"]}

    engine, ws, wt, lock, _ = first_run(repo_dir, scratch, cfg, staff, developer)
    real = engine.send

    def reviewer_down(role, request, on_input):
        if role == "reviewer":
            raise Down("timeout")
        return real(role, request, on_input)

    engine.send = reviewer_down
    try:
        with pytest.raises(Down):
            engine.run()
    finally:
        lock.close()
    ws, lock = open_workspace(scratch / "shop", "cart", rebuild=True)
    agents = Agents({("developer", "change"): developer, ("reviewer", "verify"): APPROVE})
    resumed = engine_for(ws, repo.reuse_worktree(run_record(ws)), cfg, staff, agents, resume=True)
    try:
        assert resumed.run()
    finally:
        lock.close()
    assert "developer" not in {role for role, _ in agents.requests}  # the accepted item is kept
    assert entry(ws, "change")["status"] == "accepted"


def test_a_reverted_attempt_marks_no_sub_task(tmp_path, cfg, staff):
    """A app-build develop item whose files were reverted keeps its sub-tasks open."""
    from test_engine import make_engine

    plan = resolve(catalog()["app-build"], {}, staff, cfg)
    develop = next(s for s in plan.steps() if s.id == "develop")
    assert has_subtasks(develop)
    ws = tmp_path / "ws"
    engine = make_engine(plan, cfg, tmp_path, Agents({}))
    engine.tasks.items(develop, "build", ["web"], SPEC["acceptance_criteria"])
    item = engine._items(develop)[0]
    engine._mark_subtasks(develop, item, {"criteria_done": ["AC1", "AC9"]})  # AC9 is not a criterion
    assert [(t["id"], t["done"]) for t in entry(ws, "develop", "web")["subtasks"]] == [("AC1", True),
                                                                                       ("AC2", False)]
    engine.tasks.reset_subtasks("develop", "web")
    assert not any(t["done"] for t in entry(ws, "develop", "web")["subtasks"])


# --- the module ------------------------------------------------------------------------------


def test_a_surface_item_gets_only_the_criteria_of_its_surface(tmp_path, cfg, staff):
    plan = resolve(catalog()["app-build"], {"backend": True}, staff, cfg)
    develop = next(s for s in plan.steps() if s.id == "develop")
    tl = TaskList(tmp_path)
    criteria = [{"id": "AC1", "text": "a", "surface": "web"}, {"id": "AC2", "text": "b", "surface": "api"}]
    tl.items(develop, "build", ["web", "api"], criteria)
    assert [t["id"] for t in tl.subtasks("develop", "web")] == ["AC1"]
    assert [t["id"] for t in tl.subtasks("develop", "api")] == ["AC2"]


def test_loading_a_list_puts_a_running_entry_back_to_todo(tmp_path, cfg, staff):
    plan = change_plan(cfg, staff)
    tl = TaskList(tmp_path)
    tl.plan(plan)
    tl.set("spec", None, "accepted")
    tl.set("change", None, "running")
    again = TaskList(tmp_path)
    again.plan(plan)
    again.load({("spec", None)})
    states = {e["step"]: e["status"] for e in again.as_json()["tasks"]}
    assert states == {"spec": "accepted", "change": "todo", "verify": "todo"}


def test_the_plan_lists_the_steps_with_sub_tasks(cfg, staff):
    text = plan_text(change_plan(cfg, staff))
    assert "- [ ] change (build): developer; one sub-task per acceptance criterion" in text
    assert "- [ ] spec: analyst\n" in text + "\n"


def test_factory_plan_prints_the_task_list(capsys):
    from factory import cli

    assert cli.main(["plan", "change-request"]) == 0
    out = capsys.readouterr().out
    assert "Task list" in out and "one sub-task per acceptance criterion" in out
