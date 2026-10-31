"""The runbook engine with fake agents: gates, retries, rounds, roles and checkpoints (no LLM)."""

import json
from pathlib import Path

import pytest

from factory import load_config
from factory.engine import Engine, open_workspace, triage
from factory.gates import GATES, Failure, quote_on_page, strip_html
from factory.paths import ROOT
from factory.runbook import catalog, load_staff, resolve
from test_config import ENV, base, write

PAGE = "<html><body><p>Postgres supports <b>JSONB</b> columns.[1] It is widely used.</p></body></html>"
URL = "https://example.org/pg"


@pytest.fixture
def cfg(tmp_path):
    data = base()
    data["workers"]["codex-gemini"]["enabled"] = True
    return load_config(write(tmp_path, data), ENV)


@pytest.fixture
def staff(cfg):
    return load_staff(ROOT / "agents.yaml", cfg)


class Agents:
    """Fake A2A agents: `replies[(role, task, item)]` is a list of outputs, or a callable."""

    def __init__(self, replies):
        self.replies, self.requests = replies, []

    def __call__(self, role, request, on_input):
        self.requests.append((role, request))
        key = (role, request["task"], request.get("item"))
        r = self.replies.get(key, self.replies.get((role, request["task"])))
        out = r(request) if callable(r) else r.pop(0)
        if request.get("checkpoint") and on_input:
            answer = on_input({"kind": "review", "output": out, "rendered": "draft"})
            assert answer == {"accept": True}
        return {"output": out}


def make_engine(plan, cfg, tmp_path, agents, **kw):
    ws = tmp_path / "ws"
    (ws / ".factory").mkdir(parents=True, exist_ok=True)
    kw.setdefault("interactive", False)
    return Engine(plan, cfg, ws, agents, brief="b", name="n", say=lambda m: None,
                  fetch=lambda url: strip_html(PAGE) if url == URL else None, install=False, **kw)


# --- research ---------------------------------------------------------------

PLAN = {"title": "DBs", "question": "Which DB?", "sub_questions": [
    {"id": "Q1", "question": "Does Postgres store JSON?"}, {"id": "Q2", "question": "Is it popular?"}]}
GOOD = {"claim": "Postgres has JSONB", "source_url": URL, "quote": "Postgres supports JSONB columns."}
FAKE = {"claim": "Postgres is the fastest", "source_url": URL, "quote": "Postgres is the fastest database."}
DEAD = {"claim": "x", "source_url": "https://gone.example", "quote": "anything at all here, really"}


def findings(qid, *items):
    return {"sub_question_id": qid, "answer": "yes", "findings": list(items)}


def report(md):
    return {"markdown": md}


def research_agents(report_replies, review_replies):
    return Agents({
        ("analyst", "plan"): [PLAN],
        ("researcher", "investigate", "Q1"): [findings("Q1", GOOD, FAKE)],
        ("researcher", "investigate", "Q2"): [findings("Q2", DEAD), findings("Q2", GOOD)],
        ("writer", "report"): report_replies,
        ("reviewer", "review"): review_replies,
    })


def test_research_run_keeps_only_verified_findings(cfg, staff, tmp_path):
    plan = resolve(catalog()["research"], {}, staff, cfg)
    agents = research_agents([report(f"Yes: [JSONB]({URL}).")], [{"verdict": "approve"}])
    engine = make_engine(plan, cfg, tmp_path, agents)
    assert engine.run()
    q1 = engine.artifacts["investigate"]["Q1"]
    assert [f["claim"] for f in q1["findings"]] == ["Postgres has JSONB"]
    assert q1["rejected"][0]["reason"] == "the quote is not on the page"
    # Q2's only finding cited a dead page, so the researcher was asked again, with the reason.
    q2_requests = [r for role, r in agents.requests if r.get("item") == "Q2"]
    assert len(q2_requests) == 2 and "no finding could be verified" in q2_requests[1]["feedback"]
    assert (engine.ws / "REPORT.md").read_text().startswith("Yes:")
    assert json.loads((engine.ws / "PLAN.json").read_text())["title"] == "DBs"
    run = json.loads((engine.ws / ".factory" / "run.json").read_text())
    assert run["status"] == "accepted" and run["runbook"] == "research"


def test_writer_sees_only_verified_findings(cfg, staff, tmp_path):
    plan = resolve(catalog()["research"], {"review": False}, staff, cfg)
    agents = research_agents([report(f"[a]({URL})")], [])
    make_engine(plan, cfg, tmp_path, agents).run()
    writer = next(r for role, r in agents.requests if role == "writer")
    context = "\n".join(c["body"] for c in writer["context"])
    assert "Postgres has JSONB" in context and "the fastest" not in context


def test_report_must_cite_verified_sources_and_nothing_else(cfg, staff, tmp_path):
    plan = resolve(catalog()["research"], {"review": False}, staff, cfg)
    agents = research_agents([report("No links."), report(f"[a]({URL}) [b](https://made.up)"),
                              report(f"Fixed: [a]({URL}).")], [])
    engine = make_engine(plan, cfg, tmp_path, agents)
    assert engine.run()
    feedback = [r["feedback"] for role, r in agents.requests if role == "writer"]
    assert "not linked" in feedback[1] and "not a verified source: https://made.up" in feedback[2]


def test_reviewer_rejection_starts_a_new_round_with_its_issues(cfg, staff, tmp_path):
    plan = resolve(catalog()["research"], {}, staff, cfg)
    agents = research_agents([report(f"v1 [a]({URL})"), report(f"v2 [a]({URL})")],
                             [{"verdict": "revise", "issues": ["say who it is for"]}, {"verdict": "approve"}])
    engine = make_engine(plan, cfg, tmp_path, agents)
    assert engine.run()
    second = [r for role, r in agents.requests if role == "writer"][1]
    assert "say who it is for" in second["feedback"]
    assert (engine.ws / "REPORT.md").read_text().startswith("v2")


def test_run_stops_when_rounds_run_out(cfg, staff, tmp_path):
    plan = resolve(catalog()["research"], {}, staff, cfg)
    no = {"verdict": "revise", "issues": ["no"]}
    # research's write loop has its own round count (3), so it runs out after three rejections.
    agents = research_agents([report(f"[a]({URL})")] * 3, [no, no, no])
    engine = make_engine(plan, cfg, tmp_path, agents)
    assert not engine.run()
    assert json.loads((engine.ws / ".factory" / "run.json").read_text())["status"] == "stopped"


def test_checkpoint_asks_the_person(cfg, staff, tmp_path):
    plan = resolve(catalog()["research"], {"review": False}, staff, cfg)
    agents = research_agents([report(f"[a]({URL})")], [])
    engine = make_engine(plan, cfg, tmp_path, agents, interactive=True, ask=lambda q: "")
    assert engine.run()
    assert [r["checkpoint"] for role, r in agents.requests if role == "analyst"] == [True]
    # Parallel steps never pause for a person.
    assert all(not r["checkpoint"] for role, r in agents.requests if role == "researcher")


@pytest.mark.parametrize("quote, ok", [
    ("Postgres supports JSONB columns.", True),
    ("postgres  supports “JSONB” columns", True),  # spacing, case and quote marks differ
    ("JSONB columns. It is widely used.", True),  # across a citation marker
    ("Postgres supports XML columns.", False),
    ("Postgres", False),  # too short to mean anything
])
def test_quote_matching(quote, ok):
    assert quote_on_page(quote, strip_html(PAGE)) is ok


# --- building ---------------------------------------------------------------


def write_files(cwd, files):
    for rel, text in files.items():
        (Path(cwd) / rel).parent.mkdir(parents=True, exist_ok=True)
        (Path(cwd) / rel).write_text(text)


SPEC = {"title": "t", "goal": "g", "acceptance_criteria": [{"id": "AC1", "text": "a"}]}


@pytest.fixture
def no_suites(monkeypatch):
    """Gates that run test suites and servers pass; the rest are real."""
    for g in ("tests_red", "tests_green", "serves", "counts_match"):
        monkeypatch.setitem(GATES, g, lambda ctx, out: None)


def build_engine(cfg, staff, tmp_path, develop, tester_review=None, **traits):
    plan = resolve(catalog()["web-app"], traits, staff, cfg)

    def tests(req):
        write_files(req["cwd"], {"src/App.test.jsx": "it('AC1: works', () => {})\n"})
        return {}

    replies = {
        ("analyst", "spec"): [SPEC],
        ("tester", "tests"): tests,
        ("developer", "develop"): develop,
        ("verifier", "verify"): lambda req: {"passed": 1, "failed": 0, "verdicts": {"AC1": "PASS"}},
    }
    if tester_review:
        replies[("tester", "develop:test-review")] = tester_review
    agents = Agents(replies)
    engine = make_engine(plan, cfg, tmp_path, agents)
    # A stand-in for the react-vite scaffold, without npm.
    write_files(engine.ws / "web", {"package.json": "{}", "src/main.jsx": "main", "src/App.jsx": "old"})
    return engine, agents


def test_developer_change_outside_its_role_is_reverted_and_retried(cfg, staff, tmp_path, no_suites):
    calls = []

    def develop(req):
        calls.append(req["feedback"])
        files = {"src/App.jsx": "app"}
        if len(calls) == 1:
            files["src/main.jsx"] = "hacked"
        write_files(req["cwd"], files)
        return {"summary": "done"}

    engine, _ = build_engine(cfg, staff, tmp_path, develop)
    assert engine.run()
    assert (engine.ws / "web/src/main.jsx").read_text() == (ROOT / "templates/react-vite/src/main.jsx").read_text()
    assert "outside your role: src/main.jsx" in calls[1]


def test_developer_test_change_needs_the_testers_approval(cfg, staff, tmp_path, no_suites):
    def develop(req):
        write_files(req["cwd"], {"src/App.jsx": "app", "src/App.test.jsx": "it('AC1: weaker', () => {})\n"})
        return {"summary": "done", "test_change": "AC1 query was ambiguous"}

    reviews = [{"verdict": "revise", "issues": ["it weakens AC1"]}, {"verdict": "approve"}]
    engine, agents = build_engine(cfg, staff, tmp_path, develop, tester_review=reviews)
    assert engine.run()
    review = next(r for role, r in agents.requests if r["task"] == "develop:test-review")
    assert "AC1 query was ambiguous" in json.dumps(review["context"])
    dev = [r for role, r in agents.requests if role == "developer"]
    assert "the tester rejected your change" in dev[1]["feedback"]
    assert "weaker" in (engine.ws / "web/src/App.test.jsx").read_text()  # approved the second time


def test_test_gap_stops_the_run(cfg, staff, tmp_path, no_suites):
    engine, agents = build_engine(cfg, staff, tmp_path, lambda req: {"summary": "done"})
    agents.replies[("verifier", "verify")] = lambda req: {"passed": 1, "failed": 0,
                                                          "verdicts": {"AC1": "FAIL - not checked"}}
    assert not engine.run()
    assert sum(role == "developer" for role, _ in agents.requests) == 1  # no more rounds


def test_the_build_starts_from_a_clean_scaffold(cfg, staff, tmp_path, no_suites):
    engine, _ = build_engine(cfg, staff, tmp_path, lambda req: {"summary": "done"})
    write_files(engine.ws / "web", {"src/components/Stale.jsx": "old run"})
    engine.run()
    assert not (engine.ws / "web/src/components/Stale.jsx").exists()


def test_tests_must_be_named_after_criteria(cfg, staff, tmp_path, no_suites):
    engine, agents = build_engine(cfg, staff, tmp_path, lambda req: {"summary": "done"})
    written = []

    def tests(req):
        written.append(req["feedback"])
        name = "AC1: works" if len(written) > 1 else "works"
        write_files(req["cwd"], {"src/App.test.jsx": f"it('{name}', () => {{}})\n"})
        return {}

    agents.replies[("tester", "tests")] = tests
    assert engine.run()
    assert "no test named for AC1" in written[1]


def test_full_stack_runs_each_surface_in_its_own_directory(cfg, staff, tmp_path, no_suites):
    plan = resolve(catalog()["web-app"], {"backend": True}, staff, cfg)
    spec = {**SPEC, "acceptance_criteria": [{"id": "AC1", "text": "ui", "surface": "web"},
                                            {"id": "AC2", "text": "api", "surface": "api"}]}
    contract = {"endpoints": [{"method": "GET", "path": "/api/items", "response": []}]}

    def tests(req):
        files = {"web": {"src/App.test.jsx": "it('AC1: ui', () => {})\n"},
                 "api": {"tests/test_api.py": "def test_ac2_api(client):\n    pass\n"}}[req["item"]]
        write_files(req["cwd"], files)
        return {}

    agents = Agents({
        ("analyst", "spec"): [spec], ("architect", "contract"): [contract], ("tester", "tests"): tests,
        ("developer", "develop"): lambda req: {"summary": req["item"]},
        ("verifier", "verify"): lambda req: {"passed": 1, "failed": 0, "verdicts": {
            "AC1" if req["item"] == "web" else "AC2": "PASS"}},
    })
    engine = make_engine(plan, cfg, tmp_path, agents)
    write_files(engine.ws / "web", {"package.json": "{}"})
    write_files(engine.ws / "api", {"pyproject.toml": ""})
    assert engine.run()
    dev = {r["item"]: r for role, r in agents.requests if role == "developer"}
    assert dev["web"]["cwd"].endswith("/web") and dev["api"]["cwd"].endswith("/api")
    assert "API designer" not in dev["web"]["stack"] and "fetch" in dev["web"]["stack"]
    assert "pytest" in dev["api"]["stack"] and dev["api"]["commands"]["test"].endswith("pytest -q")
    assert any("api_contract" in c["title"] or "contract" in c["title"] for c in dev["web"]["context"])
    assert json.loads((engine.ws / "API.json").read_text())["endpoints"][0]["path"] == "/api/items"


def test_only_failed_surfaces_are_rebuilt(cfg, staff, tmp_path, no_suites, monkeypatch):
    plan = resolve(catalog()["web-app"], {"backend": True}, staff, cfg)
    spec = {**SPEC, "acceptance_criteria": [{"id": "AC1", "text": "ui", "surface": "web"},
                                            {"id": "AC2", "text": "api", "surface": "api"}]}
    green = {"web": [False, True], "api": [True]}

    def tests_green(ctx, out):
        if not green[ctx.dirs[0]].pop(0):
            raise Failure("1 test still fails", "round")

    monkeypatch.setitem(GATES, "tests_green", tests_green)
    monkeypatch.setitem(GATES, "tests_named", lambda ctx, out: None)
    agents = Agents({
        ("analyst", "spec"): [spec], ("architect", "contract"): [{"endpoints": [{"method": "GET", "path": "/api/x"}]}],
        ("tester", "tests"): lambda req: {}, ("developer", "develop"): lambda req: {},
        ("verifier", "verify"): lambda req: {"passed": 1, "failed": 0, "verdicts": {"AC1": "PASS", "AC2": "PASS"}},
    })
    engine = make_engine(plan, cfg, tmp_path, agents)
    assert engine.run()
    dev = [r["item"] for role, r in agents.requests if role == "developer"]
    assert sorted(dev[:2]) == ["api", "web"] and dev[2:] == ["web"]


# --- audit ------------------------------------------------------------------


def test_audit_keeps_findings_whose_evidence_is_in_the_code(cfg, staff, tmp_path):
    target = tmp_path / "code"
    write_files(target, {"app.py": "import os\n\n\ndef run(cmd):\n    os.system(cmd)\n"})
    real = {"severity": "high", "title": "shell injection", "file": "app.py", "line": 5,
            "evidence": "os.system(cmd)", "recommendation": "use subprocess with a list"}
    made_up = {**real, "title": "eval", "evidence": "eval(user_input)"}
    outside = {**real, "title": "escape", "file": "../../etc/passwd"}

    def audit(req):
        return {"lens": req["item"], "summary": "s", "findings": [real, made_up, outside] if req["item"] == "security" else []}

    plan = resolve(catalog()["code-audit"], {}, staff, cfg)
    agents = Agents({("auditor", "audit"): audit, ("writer", "report"): [report("# Audit")],
                     ("reviewer", "review"): [{"verdict": "approve"}]})
    engine = make_engine(plan, cfg, tmp_path, agents, target=target)
    assert engine.run()
    sec = engine.artifacts["audit"]["security"]
    assert [f["title"] for f in sec["findings"]] == ["shell injection"]
    assert {r["title"]: r["reason"] for r in sec["rejected"]} == {
        "eval": "the evidence is not near line 5", "escape": "no such file in the project"}
    assert all(r["cwd"] == str(target) for role, r in agents.requests if role == "auditor")


def test_audit_needs_a_target(cfg, staff, tmp_path):
    from factory import ConfigError

    plan = resolve(catalog()["code-audit"], {}, staff, cfg)
    with pytest.raises(ConfigError, match="--target"):
        make_engine(plan, cfg, tmp_path, Agents({}))


def test_agents_that_may_not_write_have_stray_files_reverted(cfg, staff, tmp_path):
    plan = resolve(catalog()["research"], {"review": False}, staff, cfg)
    agents = research_agents([report(f"[a]({URL})")], [])
    original = agents.replies[("analyst", "plan")]

    def plan_and_litter(req):
        write_files(req["cwd"], {"notes.txt": "scratch"})
        return original[0]

    agents.replies[("analyst", "plan")] = plan_and_litter
    engine = make_engine(plan, cfg, tmp_path, agents)
    assert engine.run() and not (engine.ws / "notes.txt").exists()


# --- triage and workspaces --------------------------------------------------


def test_triage_returns_a_checked_choice(tmp_path):
    books = catalog()
    send = Agents({("triage", "triage"): [
        {"runbook": "web-app", "traits": {"backend": True}, "name": "family-list", "rationale": "shared"}]})
    out = triage(send, "a shared shopping list", books, target=None, on_input=None, cwd=tmp_path)
    assert out["runbook"] == "web-app" and out["traits"] == {"backend": True}
    req = send.requests[0][1]
    assert "research" in req["context"][1]["body"] and req["output"] == "triage"


def test_triage_choice_outside_the_catalog_is_an_error(tmp_path):
    send = Agents({("triage", "triage"): [{"runbook": "poem", "name": "x", "rationale": "r"}]})
    with pytest.raises(ValueError, match="not one of"):
        triage(send, "b", catalog(), target=None, on_input=None, cwd=tmp_path)


def test_workspace_lock_and_rebuild(tmp_path):
    from factory import ConfigError

    ws, lock = open_workspace(tmp_path, "todo", rebuild=False)
    with pytest.raises(ConfigError, match="already being worked on"):
        open_workspace(tmp_path, "todo", rebuild=False)
    lock.close()
    (ws / ".factory" / "run.json").write_text(json.dumps({"status": "accepted"}))
    with pytest.raises(ConfigError, match="--rebuild"):
        open_workspace(tmp_path, "todo", rebuild=False)
    open_workspace(tmp_path, "todo", rebuild=True)[1].close()
    with pytest.raises(ConfigError):
        open_workspace(tmp_path, "../x", rebuild=False)
