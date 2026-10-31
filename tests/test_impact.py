"""The impact-analysis runbook: service links from repos read in place (no LLM, no network).

The repos are real git repos in tmp_path. The agents are the fakes of test_engine.py. A reader must
leave every repo as it was, and the tests check that on real git state.
"""

import argparse
import json

import pytest

from factory import ConfigError, schemas
from factory import cli
from factory.gates import GATES, Failure
from factory.runbook import catalog, resolve
from test_engine import Agents, make_engine, write_files
from test_repo import APPROVE, gate_ctx, git, scratch  # noqa: F401  (fixture)
from test_usage import cfg, staff  # noqa: F401  (fixtures)

CLIENT = "import os\n\nPAYMENTS_URL = os.environ.get('PAYMENTS_URL', 'http://payments:8080')\n\n\n" \
         "def charge(order):\n    return post(f\"{PAYMENTS_URL}/charge\", order)\n"
CLIENT_CALL = 'return post(f"{PAYMENTS_URL}/charge", order)'  # on line 7 of CLIENT
APP = "def charge(order):\n    return order\n"


def make_repo(path, files):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    write_files(path, files)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    return path.resolve()


@pytest.fixture
def repos(tmp_path):
    """Two services: orders calls payments, and payments publishes an event for orders."""
    orders = make_repo(tmp_path / "orders", {"client.py": CLIENT, "README.md": "orders\n"})
    payments = make_repo(tmp_path / "payments", {"app.py": APP, "events.py": "emit('payment.done')\n"})
    return [orders, payments]


def snapshot_of(path):
    """Everything git can see in a repo: HEAD, the status and the files."""
    return (git(path, "rev-parse", "HEAD"), git(path, "status", "--porcelain", "--untracked-files=all"),
            sorted((p.relative_to(path).as_posix(), p.read_bytes()) for p in path.rglob("*")
                   if p.is_file() and ".git" not in p.parts))


def link(kind, target, file, line, evidence):
    return {"kind": kind, "target": target, "file": file, "line": line, "evidence": evidence}


ORDERS_LINKS = {"service": "orders", "summary": "orders calls payments",
                "links": [link("calls", "payments", "client.py", 7, CLIENT_CALL)]}
PAYMENTS_LINKS = {"service": "payments", "summary": "payments publishes payment.done",
                  "links": [link("publishes", "payment.done", "events.py", 1, "emit('payment.done')")]}
REPORT = {"markdown": "# Impact\n\nA change to charge touches orders and payments."}


def reply(value, default):
    """A fake reply: a function is called as is, anything else is one reply."""
    if callable(value):
        return value
    return [value or default]


def impact_agents(orders=None, payments=None):
    return Agents({
        ("auditor", "links", "orders"): reply(orders, ORDERS_LINKS),
        ("auditor", "links", "payments"): reply(payments, PAYMENTS_LINKS),
        ("writer", "impact"): [REPORT],
        ("reviewer", "review"): APPROVE,
    })


def impact_engine(tmp_path, cfg, staff, agents, repos):
    plan = resolve(catalog()["impact-analysis"], {}, staff, cfg)
    return make_engine(plan, cfg, tmp_path, agents, repos=repos)


def run_events(ws):
    return json.loads((ws / ".factory" / "run.json").read_text())["events"]


# --- the links gate ---------------------------------------------------------------------------


def test_a_link_whose_evidence_is_near_its_line_is_kept(tmp_path):
    write_files(tmp_path, {"client.py": CLIENT})
    out = {"links": [link("calls", "payments", "client.py", 7, CLIENT_CALL)]}
    GATES["link_verified"](gate_ctx(tmp_path, None), out)
    assert [x["verified"] for x in out["links"]] == [True]
    assert out.get("rejected") == []


def test_a_link_whose_evidence_is_not_in_the_file_is_rejected(tmp_path):
    write_files(tmp_path, {"client.py": CLIENT})
    out = {"links": [link("calls", "billing", "client.py", 7, "requests.post(BILLING)"),
                     link("calls", "payments", "client.py", 7, CLIENT_CALL)]}
    GATES["link_verified"](gate_ctx(tmp_path, None), out)
    assert [x["target"] for x in out["links"]] == ["payments"]
    assert out["rejected"][0]["target"] == "billing"
    assert out["rejected"][0]["verified"] is False
    assert "not near line 7" in out["rejected"][0]["reason"]


def test_a_link_citing_a_file_outside_the_repo_is_rejected(tmp_path):
    repo_root = tmp_path / "orders"
    write_files(repo_root, {"client.py": CLIENT})
    write_files(tmp_path, {"secret.py": CLIENT})  # next to the repo, not in it
    out = {"links": [link("calls", "payments", "../secret.py", 7, CLIENT_CALL)]}
    with pytest.raises(Failure, match="no link could be verified") as info:
        GATES["link_verified"](gate_ctx(repo_root, None), out)
    assert info.value.level == "step"
    assert out["links"] == []
    assert "no such file in this repo" in out["rejected"][0]["reason"]


def test_when_every_link_is_rejected_the_step_fails_with_the_reasons(tmp_path):
    write_files(tmp_path, {"client.py": CLIENT})
    out = {"links": [link("calls", "billing", "client.py", 7, "requests.post(BILLING)")]}
    with pytest.raises(Failure, match="Copy the evidence exactly") as info:
        GATES["link_verified"](gate_ctx(tmp_path, None), out)
    assert "calls billing: the evidence is not near line 7" in info.value.message


def test_a_repo_with_no_links_passes_the_gate(tmp_path):
    out = {"links": []}
    GATES["link_verified"](gate_ctx(tmp_path, None), out)
    assert out["links"] == []


# --- the schema and the rendering -------------------------------------------------------------


def test_a_link_must_name_a_known_kind():
    data = {"service": "orders", "links": [link("phones", "payments", "client.py", 7, "x")]}
    with pytest.raises(ValueError, match="does not match the service_links shape"):
        schemas.validate("service_links", data)


def test_a_link_line_must_be_a_real_line():
    data = {"service": "orders", "links": [link("calls", "payments", "client.py", 0, "x")]}
    with pytest.raises(ValueError, match="line"):
        schemas.validate("service_links", data)


def test_render_lists_the_links_and_says_when_there_are_none():
    text = schemas.render("service_links", ORDERS_LINKS)
    assert "calls payments (client.py:7)" in text
    empty = schemas.render("service_links", {"service": "orders", "summary": "alone", "links": []})
    assert "no links found" in empty


# --- a run on two repos -----------------------------------------------------------------------


def test_a_run_reads_both_repos_and_writes_the_report(tmp_path, cfg, staff, repos):
    before = [snapshot_of(r) for r in repos]
    agents = impact_agents()
    engine = impact_engine(tmp_path, cfg, staff, agents, repos)
    assert engine.run()
    ws = tmp_path / "ws"
    assert (ws / "IMPACT.md").read_text().startswith("# Impact")
    assert [snapshot_of(r) for r in repos] == before  # no repo changed


def test_each_repo_is_read_in_place_by_its_own_auditor(tmp_path, cfg, staff, repos):
    agents = impact_agents()
    impact_engine(tmp_path, cfg, staff, agents, repos).run()
    reads = {r["item"]: r["cwd"] for role, r in agents.requests if role == "auditor"}
    assert reads == {"orders": str(repos[0]), "payments": str(repos[1])}


def test_the_links_of_both_repos_reach_the_writer(tmp_path, cfg, staff, repos):
    agents = impact_agents()
    impact_engine(tmp_path, cfg, staff, agents, repos).run()
    context = next(r for role, r in agents.requests if role == "writer")["context"]
    body = "\n".join(c["body"] for c in context)
    assert "calls payments (client.py:7)" in body
    assert "publishes payment.done (events.py:1)" in body


def test_a_reader_that_changes_a_repo_has_the_change_reverted(tmp_path, cfg, staff, repos):
    def sneaky(req):
        write_files(req["cwd"], {"scratch.txt": "notes\n", "app.py": "def charge(order):\n    return 0\n"})
        git(req["cwd"], "add", "-A")
        git(req["cwd"], "commit", "-q", "-m", "a reader's commit")
        return PAYMENTS_LINKS

    before = [snapshot_of(r) for r in repos]
    agents = impact_agents(payments=sneaky)
    engine = impact_engine(tmp_path, cfg, staff, agents, repos)
    assert engine.run()
    assert [snapshot_of(r) for r in repos] == before
    reverted = [e for e in run_events(tmp_path / "ws") if e["status"] == "files reverted"]
    assert reverted and reverted[0]["item"] == "payments"
    assert "scratch.txt" in reverted[0]["detail"]


def test_a_link_with_wrong_evidence_is_dropped_before_the_writer(tmp_path, cfg, staff, repos):
    bad = {"service": "orders", "summary": "x", "links": [
        link("calls", "billing", "client.py", 7, "requests.post(BILLING)"),
        link("calls", "payments", "client.py", 7, CLIENT_CALL)]}
    agents = impact_agents(orders=bad)
    assert impact_engine(tmp_path, cfg, staff, agents, repos).run()
    context = next(r for role, r in agents.requests if role == "writer")["context"]
    body = "\n".join(c["body"] for c in context)
    assert "calls billing" not in body
    assert "calls payments" in body


def test_the_run_record_lists_the_repos_it_read(tmp_path, cfg, staff, repos):
    impact_engine(tmp_path, cfg, staff, impact_agents(), repos).run()
    record = json.loads((tmp_path / "ws" / ".factory" / "run.json").read_text())
    assert record["repos"] == [str(r) for r in repos]
    assert record["status"] == "accepted"
    assert "branch" not in record and "commit" not in record


def test_the_engine_refuses_this_runbook_without_repos(tmp_path, cfg, staff):
    from factory.engine import Engine

    plan = resolve(catalog()["impact-analysis"], {}, staff, cfg)
    ws = tmp_path / "ws"
    (ws / ".factory").mkdir(parents=True)
    with pytest.raises(ConfigError, match="needs --repo"):
        Engine(plan, cfg, ws, impact_agents(), brief="b", name="n", interactive=False, say=lambda m: None,
               install=False)


# --- the command line -------------------------------------------------------------------------


def args(**kw):
    base = {"repo": [], "base": None, "test_command": None, "target": None, "name": None}
    return argparse.Namespace(**{**base, **kw})


def test_the_impact_runbook_needs_a_repo(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "APPS_DIR", tmp_path / "apps")
    assert cli.main(["run", "--runbook", "impact-analysis", "--brief", "x", "--yes"]) == 2
    assert "pass --repo PATH once for each repo" in capsys.readouterr().err


@pytest.mark.parametrize("flag, value", [("--test-command", "npm test"), ("--base", "main")])
def test_the_impact_runbook_refuses_flags_for_a_change(monkeypatch, tmp_path, capsys, repos, flag, value):
    monkeypatch.setattr(cli, "APPS_DIR", tmp_path / "apps")
    code = cli.main(["run", "--runbook", "impact-analysis", "--repo", str(repos[0]), flag, value,
                     "--brief", "x", "--yes"])
    assert code == 2
    assert "do not apply to impact-analysis" in capsys.readouterr().err


def test_two_repos_with_one_folder_name_are_refused(tmp_path, repos):
    other = make_repo(tmp_path / "other" / "orders", {"a.py": "x\n"})
    with pytest.raises(ConfigError, match="two --repo folders have the name orders"):
        cli._read_repos(args(repo=[str(repos[0]), str(other)]))


def test_a_folder_inside_a_repo_is_refused(repos):
    (repos[0] / "src").mkdir()
    with pytest.raises(ConfigError, match="must be the top folder of a git repo"):
        cli._read_repos(args(repo=[str(repos[0] / "src")]))


def test_a_repo_without_commits_is_refused(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", "-b", "main")
    with pytest.raises(ConfigError, match="has no commits yet"):
        cli._read_repos(args(repo=[str(empty)]))


def test_the_impact_runbook_reads_the_repos_and_keeps_its_run_in_apps(repos):
    rb = catalog()["impact-analysis"]
    assert cli._read_repos(args(repo=[str(r) for r in repos])) == repos
    workdir, top = cli._workdir(args(repo=[str(repos[0])]), rb)
    assert (workdir, top) == (cli.APPS_DIR, None)


def test_resume_finds_an_impact_run_in_apps(monkeypatch, tmp_path, repos, scratch):
    monkeypatch.setattr(cli, "APPS_DIR", tmp_path / "apps")
    run = tmp_path / "apps" / "impact" / ".factory"
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}")
    assert cli._find_home(args(repo=[str(repos[0])], name="impact")) == tmp_path / "apps"
