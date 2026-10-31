"""Resume (P12): a stopped run continues from its record, keeps what it accepted and runs the rest (no LLM)."""

import json

import pytest

from factory import ConfigError
from factory.engine import Engine
from factory.runbook import catalog, resolve
from test_engine import PLAN, URL, Agents, findings, make_engine, research_agents, report, GOOD, DEAD
from test_usage import cfg, staff  # noqa: F401  (fixtures)


def research_plan(cfg, staff):
    return resolve(catalog()["research"], {"review": False}, staff, cfg)


def test_a_resumed_run_does_not_repeat_accepted_items(cfg, staff, tmp_path):
    plan = research_plan(cfg, staff)
    # First attempt: the writer produces nothing usable, so the run stops after the research.
    first = research_agents([report(f"[a]({URL})")], [])
    first.replies[("writer", "report")] = [RuntimeError_of("no report")]
    engine = make_engine(plan, cfg, tmp_path, Crashing(first.replies))
    with pytest.raises(RuntimeError):
        engine.run()
    saved = json.loads((engine.ws / ".factory" / "run.json").read_text())
    assert saved["status"] == "error"
    assert {e["step"] for e in saved["events"] if e["status"] == "accepted"} >= {"plan", "investigate"}

    # Resume with a working writer: the plan and the research are kept, not asked again.
    agents = research_agents([report(f"[a]({URL})")], [])
    resumed = Engine(plan, cfg, engine.ws, agents, brief="b", name="n", interactive=False,
                     say=lambda m: None, fetch=lambda url: None, install=False,
                     run_id=saved["run_id"], resume=True)
    assert resumed.run()
    asked = {r["task"] for _, r in agents.requests}
    assert "plan" not in asked and "investigate" not in asked and "report" in asked
    run = json.loads((engine.ws / ".factory" / "run.json").read_text())
    assert run["status"] == "accepted" and run["run_id"] == saved["run_id"]
    assert len(run["resumes"]) == 1 and run["resumes"][0]["factory"] is not None
    assert (engine.ws / "REPORT.md").read_text().startswith("[a]")
    assert run["events"][:len(saved["events"])] == saved["events"]  # the earlier history is kept


def test_resume_refuses_a_run_for_another_runbook(cfg, staff, tmp_path):
    plan = research_plan(cfg, staff)
    engine = make_engine(plan, cfg, tmp_path, research_agents([report(f"[a]({URL})")], []))
    assert engine.run()
    run = json.loads((engine.ws / ".factory" / "run.json").read_text())
    run["runbook"] = "code-audit"
    (engine.ws / ".factory" / "run.json").write_text(json.dumps(run))
    with pytest.raises(ConfigError, match="not code-audit|for runbook"):
        Engine(plan, cfg, engine.ws, research_agents([], []), brief="b", name="n", interactive=False,
               say=lambda m: None, fetch=lambda url: None, install=False, resume=True)


def test_resume_needs_an_existing_record(cfg, staff, tmp_path):
    plan = research_plan(cfg, staff)
    (tmp_path / "ws" / ".factory").mkdir(parents=True)
    with pytest.raises(ConfigError, match="nothing to resume"):
        Engine(plan, cfg, tmp_path / "ws", research_agents([], []), brief="b", name="n",
               interactive=False, say=lambda m: None, fetch=lambda url: None, install=False, resume=True)


def test_the_cli_refuses_to_resume_an_accepted_run(monkeypatch, tmp_path, capsys):
    from factory import cli

    apps = tmp_path / "apps"
    (apps / "todo" / ".factory").mkdir(parents=True)
    (apps / "todo" / ".factory" / "run.json").write_text(json.dumps({"status": "accepted", "runbook": "research"}))
    monkeypatch.setattr(cli, "APPS_DIR", apps)
    assert cli.main(["run", "--resume", "--name", "todo", "--brief", "x"]) == 2
    assert "already accepted" in capsys.readouterr().err


def test_the_cli_resume_needs_a_name(capsys):
    from factory import cli

    assert cli.main(["run", "--resume", "--brief", "x"]) == 2
    assert "--name" in capsys.readouterr().err


class Crashing(Agents):
    """Like Agents, but a reply that is an exception is raised."""

    def __call__(self, role, request, on_input):
        key = (role, request["task"], request.get("item"))
        r = self.replies.get(key, self.replies.get((role, request["task"])))
        if isinstance(r, list) and r and isinstance(r[0], RuntimeError_of):
            raise r.pop(0).exc
        return super().__call__(role, request, on_input)


class RuntimeError_of:
    def __init__(self, msg):
        self.exc = RuntimeError(msg)
