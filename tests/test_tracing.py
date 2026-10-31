"""Run ID and run record: one ID in every request, in run.json and in each agent log line,
and the factory commit and gate names that accepted the work (no LLM)."""

import json
import re

import pytest

from factory import load_config
from factory.agent import handle
from factory.engine import factory_version, gate_names, new_run_id, triage
from factory.paths import ROOT
from factory.roles import ROLES
from factory.runbook import catalog, load_staff, resolve
from test_config import ENV, base, default_cfg, write
from test_engine import URL, make_engine, research_agents, report


@pytest.fixture
def cfg(tmp_path):
    data = base()
    data["workers"]["codex-gemini"]["enabled"] = True
    return load_config(write(tmp_path, data), ENV)


@pytest.fixture
def staff(cfg):
    return load_staff(ROOT / "agents.yaml", cfg)

ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")


def read_run(engine):
    return json.loads((engine.ws / ".factory" / "run.json").read_text())


def research_run(cfg, staff, tmp_path, **kw):
    plan = resolve(catalog()["research"], {"review": False}, staff, cfg)
    agents = research_agents([report(f"[a]({URL})")], [])
    return make_engine(plan, cfg, tmp_path, agents, **kw), agents, plan


def test_a_run_id_looks_like_a_time_and_a_random_suffix_and_is_new_each_time():
    assert ID.fullmatch(new_run_id()) and new_run_id() != new_run_id()


def test_every_request_and_run_json_carry_the_run_id(cfg, staff, tmp_path):
    engine, agents, _ = research_run(cfg, staff, tmp_path, run_id="20261031-000000-abcdef")
    assert engine.run()
    assert read_run(engine)["run_id"] == "20261031-000000-abcdef"
    assert agents.requests and {r["run_id"] for _, r in agents.requests} == {"20261031-000000-abcdef"}


def test_the_engine_makes_a_run_id_when_none_is_given(cfg, staff, tmp_path):
    engine, agents, _ = research_run(cfg, staff, tmp_path)
    assert engine.run() and ID.fullmatch(read_run(engine)["run_id"])
    assert {r["run_id"] for _, r in agents.requests} == {engine.run_id}


def test_the_triage_request_carries_the_run_id(tmp_path):
    seen = []

    def send(role, request, on_input):
        seen.append(request)
        return {"output": {"runbook": "research", "name": "x", "rationale": "r"}}

    triage(send, "b", catalog(), target=None, on_input=None, cwd=tmp_path, run_id="the-run")
    assert seen[0]["run_id"] == "the-run"


def test_run_json_records_the_factory_commit_and_the_gates_of_each_step(cfg, staff, tmp_path):
    engine, _, plan = research_run(cfg, staff, tmp_path)
    assert engine.run()
    run = read_run(engine)
    assert set(run["factory"]) == {"commit", "dirty"}
    assert run["gates"]["investigate"] == ["citations_verified"]
    assert run["gates"]["report"] == ["sources_cited"] and run["gates"] == gate_names(plan)
    assert "write" in run["gates"]  # the loop itself


def test_factory_version_is_a_commit_hash_in_this_checkout():
    v = factory_version()
    assert re.fullmatch(r"[0-9a-f]{40}", v["commit"]) and isinstance(v["dirty"], bool)


def test_an_agent_prints_the_run_id_for_each_request(monkeypatch, capsys, tmp_path):
    from factory import agent

    monkeypatch.setattr(agent, "produce", lambda *a: ({"summary": "ok"}, []))
    req = {"run_id": "the-run", "task": "report", "item": "Q1", "instructions": "x", "context": [],
           "output": None, "cwd": str(tmp_path), "commands": {}}
    handle(default_cfg(), ROLES["writer"], "w", "m", [{"role": "user", "data": req}])
    assert "run the-run: report [Q1], turn 1" in capsys.readouterr().out


# --- a request that fits no runbook (P17) -----------------------------------


class FakeFleet:
    """Stands in for Fleet: its send() answers triage with a canned reply."""

    reply: dict = {}

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def send(self, role, request, on_input=None):
        return {"output": self.reply}


def run_cli(monkeypatch, tmp_path, reply, argv):
    from factory import cli

    FakeFleet.reply = reply
    monkeypatch.setattr(cli, "Fleet", FakeFleet)
    monkeypatch.setattr(cli, "APPS_DIR", tmp_path / "apps")
    return cli.main(["run", "--brief", "what is a healthy diet?", *argv])


def test_a_request_that_fits_no_runbook_prints_a_disclaimer_and_makes_no_workspace(
        monkeypatch, tmp_path, capsys):
    code = run_cli(monkeypatch, tmp_path, {"runbook": "none", "rationale": "it is a health question"}, [])
    out = capsys.readouterr().out
    assert code == 1
    assert "There is no runbook for this request" in out and "it is a health question" in out
    assert not (tmp_path / "apps").exists()


def test_a_request_that_fits_a_runbook_still_gets_a_plan(monkeypatch, tmp_path, capsys):
    reply = {"runbook": "research", "name": "diet", "rationale": "a question to answer"}
    monkeypatch.setattr("builtins.input", lambda *_: "n")  # decline the plan: the run stops there
    code = run_cli(monkeypatch, tmp_path, reply, [])
    out = capsys.readouterr().out
    assert code == 1 and "There is no runbook" not in out
    assert "Runbook: research" in out and "Not started." in out
