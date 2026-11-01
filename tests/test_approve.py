"""PRD FR-25: commands never prompt. A checkpoint or a push waits at `awaiting: approval`, and
`factory approve` answers it. The record says who approved: a person, or --yes. No LLM.
"""

import json

import pytest

from factory import cli
from factory.engine import Engine
from test_engine import PAGE, URL, Agents, make_engine, report, research_agents, strip_html
from test_resume import research_plan
from test_usage import cfg, staff  # noqa: F401  (fixtures)


def record_of(ws):
    return json.loads((ws / ".factory" / "run.json").read_text())


def answer(ws, decision="accept", changes=None):
    """What `factory approve` writes: the person's decision on the point the run waits at."""
    rec = record_of(ws)
    rec["approvals"] = [*rec.get("approvals", []), {
        "point": rec["awaiting"]["point"], "decision": decision, "by": "person", "at": "now",
        **({"changes": changes} if changes else {})}]
    (ws / ".factory" / "run.json").write_text(json.dumps(rec))


def resumed(plan, cfg, ws, agents):
    return Engine(plan, cfg, ws, agents, brief="b", name="n", say=lambda m: None, wait_at_checkpoints=True,
                  fetch=lambda url: strip_html(PAGE) if url == URL else None, install=False, resume=True)


def test_yes_approves_the_checkpoint_and_the_record_says_so(cfg, staff, tmp_path):
    engine = make_engine(research_plan(cfg, staff), cfg, tmp_path, research_agents([report(f"[a]({URL})")], []))
    assert engine.run()
    assert [(a["point"], a["decision"], a["by"]) for a in record_of(engine.ws)["approvals"]] == [
        ("plan", "accept", "--yes")]


def test_a_person_approves_the_checkpoint_and_the_run_goes_on(cfg, staff, tmp_path):
    plan = research_plan(cfg, staff)
    first = research_agents([], [])
    engine = make_engine(plan, cfg, tmp_path, first, wait_at_checkpoints=True)
    assert not engine.run()
    assert (engine.ws / "PLAN.json").exists()  # the draft is saved for the person to read
    answer(engine.ws)
    agents = research_agents([report(f"[a]({URL})")], [])
    assert resumed(plan, cfg, engine.ws, agents).run()
    assert "analyst" not in {role for role, _ in agents.requests}  # the approved plan is kept
    rec = record_of(engine.ws)
    assert rec["status"] == "accepted" and "awaiting" not in rec
    assert [(a["point"], a["by"]) for a in rec["approvals"]] == [("plan", "person")]


def test_changes_send_the_draft_back_with_the_person_s_words(cfg, staff, tmp_path):
    plan = research_plan(cfg, staff)
    engine = make_engine(plan, cfg, tmp_path, research_agents([], []), wait_at_checkpoints=True)
    engine.run()
    answer(engine.ws, "changes", "add a question about cost")
    agents = research_agents([], [])
    again = resumed(plan, cfg, engine.ws, agents)
    assert not again.run()  # the new draft waits for a review too
    req = next(r for role, r in agents.requests if role == "analyst")
    assert "add a question about cost" in req["feedback"] and "Your previous draft" in req["feedback"]
    assert record_of(engine.ws)["awaiting"]["point"] == "plan"
    answer(engine.ws)
    final = research_agents([report(f"[a]({URL})")], [])
    assert resumed(plan, cfg, engine.ws, final).run()
    assert [a["decision"] for a in record_of(engine.ws)["approvals"]] == ["changes", "accept"]


def test_no_request_asks_the_person_anything(cfg, staff, tmp_path):
    agents = research_agents([report(f"[a]({URL})")], [])
    make_engine(research_plan(cfg, staff), cfg, tmp_path, agents).run()
    assert all(not r["interactive"] and not r["checkpoint"] for _, r in agents.requests)


# --- the command ---------------------------------------------------------------------------------


@pytest.fixture
def apps(monkeypatch, tmp_path):
    apps = tmp_path / "apps"
    monkeypatch.setattr(cli, "APPS_DIR", apps)
    return apps


def waiting_run(apps, status="awaiting", point="plan"):
    (apps / "r" / ".factory").mkdir(parents=True)
    rec = {"status": status, "runbook": "research", "run_id": "x", "brief": "b",
           **({"awaiting": {"point": point}} if status == "awaiting" else {})}
    (apps / "r" / ".factory" / "run.json").write_text(json.dumps(rec))
    return apps / "r" / ".factory" / "run.json"


def test_approve_records_the_person_and_continues_the_run(apps, monkeypatch):
    path = waiting_run(apps)
    seen = []
    monkeypatch.setattr(cli, "cmd_run", lambda a: seen.append(a) or 0)
    assert cli.main(["approve", "--name", "r"]) == 0
    a = json.loads(path.read_text())["approvals"][0]
    assert (a["point"], a["decision"], a["by"]) == ("plan", "accept", "person")
    assert seen[0].resume and seen[0].name == "r" and not seen[0].yes  # later checkpoints wait again


def test_approve_with_changes_records_them(apps, monkeypatch):
    path = waiting_run(apps)
    monkeypatch.setattr(cli, "cmd_run", lambda a: 0)
    assert cli.main(["approve", "--name", "r", "--changes", "fewer questions"]) == 0
    a = json.loads(path.read_text())["approvals"][0]
    assert a["decision"] == "changes" and a["changes"] == "fewer questions"


def test_approve_refuses_a_run_that_does_not_wait(apps, capsys):
    waiting_run(apps, status="stopped")
    assert cli.main(["approve", "--name", "r"]) == 1
    assert "not awaiting approval" in capsys.readouterr().err


def test_changes_do_not_apply_to_the_push(apps, capsys):
    waiting_run(apps, point="push")
    assert cli.main(["approve", "--name", "r", "--changes", "x"]) == 2
    assert "--changes answers a checkpoint" in capsys.readouterr().err


def test_a_bad_command_exits_2_and_a_failed_one_exits_1(apps, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--no-such-flag"])
    assert e.value.code == 2
    assert cli.main(["approve", "--name", "nothing-here"]) == 1  # well formed, but there is no such run
