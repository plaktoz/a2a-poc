"""`factory runs` (P9): a read-only sum of the run records under apps/ (no LLM)."""

import json

from factory import cli
from factory.runs import first_pass, load_runs, summarize


def record(app, status, runbook="research", events=(), loops=None, usage=None):
    return {"app": app, "status": status, "runbook": runbook, "events": list(events),
            "loops": loops or {}, "usage": usage or {"calls": 0, "cost_usd": 0.0, "calls_without_cost": 0,
                                                     "by_model": {}}}


def ev(step, item, status, role="writer"):
    return {"step": step, "item": item, "role": role, "status": status}


def write_run(apps, name, data):
    (apps / name / ".factory").mkdir(parents=True)
    (apps / name / ".factory" / "run.json").write_text(json.dumps(data))


def test_first_pass_counts_items_accepted_without_a_rejection():
    events = [ev("investigate", "Q1", "accepted", "researcher"),
              ev("investigate", "Q2", "rejected", "researcher"), ev("investigate", "Q2", "accepted", "researcher"),
              ev("write", None, "accepted", "loop")]
    assert first_pass(record("a", "accepted", events=events)) == (1, 2)


def test_summary_totals_status_runbooks_loops_and_models():
    usage = {"calls": 3, "cost_usd": 0.5, "calls_without_cost": 1,
             "by_model": {"claude-test": {"calls": 2, "input_tokens": 100, "output_tokens": 10,
                                          "cost_usd": 0.5, "calls_without_cost": 0},
                          "gpt": {"calls": 1, "input_tokens": 50, "output_tokens": 5,
                                  "cost_usd": 0.0, "calls_without_cost": 1}}}
    runs = [record("a", "accepted", loops={"write": {"rounds_used": 2, "accepted": True}},
                   events=[ev("plan", None, "accepted", "analyst")], usage=usage),
            record("b", "stopped", runbook="code-audit", loops={"write": {"rounds_used": 3, "accepted": False}})]
    s = summarize(runs)
    assert s["runs"] == 2 and s["by_status"] == {"accepted": 1, "stopped": 1}
    assert s["by_runbook"]["research"] == {"runs": 1, "accepted": 1, "cost_usd": 0.5}
    assert s["loop_rounds"]["write"] == {"loops": 2, "rounds": 5, "accepted": 1}
    assert s["usage"] == {"calls": 3, "cost_usd": 0.5, "calls_without_cost": 1}
    assert s["by_model"]["claude-test"]["cost_usd"] == 0.5 and s["by_model"]["gpt"]["calls_without_cost"] == 1
    assert s["first_pass"] == {"first_attempt": 1, "accepted_items": 1, "rate": 1.0}


def test_an_empty_apps_folder_says_so(tmp_path, capsys):
    from factory.runs import view

    assert view(summarize([]), [])["runs"] == "0 found"


def test_unreadable_records_are_reported_not_fatal(tmp_path):
    write_run(tmp_path, "good", {"status": "accepted", "runbook": "research"})
    (tmp_path / "bad" / ".factory").mkdir(parents=True)
    (tmp_path / "bad" / ".factory" / "run.json").write_text("{not json")
    records, unreadable = load_runs(tmp_path)
    assert [r["app"] for r in records] == ["good"] and unreadable == ["bad"]


def test_the_command_is_read_only_and_prints_json(monkeypatch, tmp_path, capsys):
    write_run(tmp_path, "a", {"status": "accepted", "runbook": "research", "run_id": "x"})
    monkeypatch.setattr(cli, "APPS_DIR", tmp_path)
    before = (tmp_path / "a" / ".factory" / "run.json").read_text()
    assert cli.main(["runs", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["runs"] == 1
    assert (tmp_path / "a" / ".factory" / "run.json").read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a"]  # nothing was created
