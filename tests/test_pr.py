"""P10 slice 2: push the branch and open a pull request, only with approval (no network).

`origin` is a bare git repo in tmp_path, so `git push` is real and never leaves the machine. `gh` is a
fake: it records its arguments and prints a URL.
"""

import subprocess
from types import SimpleNamespace

import pytest

from factory import ConfigError, cli, repo
from factory.engine import Engine, open_workspace
from test_repo import (APPROVE, SPEC, change_agents, change_plan, git, make_change,  # noqa: F401  (fixtures)
                       repo_dir, run_record, scratch)
from test_usage import cfg, staff  # noqa: F401  (fixtures)
from test_engine import Agents, strip_html

WRITES = {"app.py": "def cart():\n    return ['a']\n"}
URL = "https://github.com/acme/shop/pull/7"


@pytest.fixture
def origin(tmp_path, repo_dir):
    """A bare repo as the remote `origin` of the shop repo."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(repo_dir, "remote", "add", "origin", str(bare))
    return bare


@pytest.fixture
def gh(monkeypatch):
    """Replace the `gh` call. The fake records its argv and prints a PR URL, or fails when told to."""
    calls = SimpleNamespace(argv=[], fail=None)
    real = repo.run

    def fake(argv, **kw):
        if argv[0] != "gh":
            return real(argv, **kw)
        calls.argv.append(argv)
        if calls.fail:
            return subprocess.CompletedProcess(argv, 1, "", calls.fail)
        return subprocess.CompletedProcess(argv, 0, f"Creating pull request\n{URL}\n", "")

    monkeypatch.setattr(repo, "run", fake)
    return calls


def start(tmp_path, repo_dir, scratch, cfg, staff, *, approved=False, agents=None, feature="cart"):
    """A change-request run with --pr, started the way the CLI does."""
    ws, lock = open_workspace(scratch / repo_dir.name, feature, rebuild=False)
    wt = repo.open_worktree(repo_dir, feature, None, ws)
    return build(tmp_path, ws, wt, cfg, staff, agents or change_agents(dev_writes=WRITES),
                 approved=approved), ws, lock


def build(tmp_path, ws, wt, cfg, staff, agents, *, approved=False, resume=False, pr=True, say=lambda m: None):
    return Engine(change_plan(cfg, staff), cfg, ws, agents, brief="add a cart", name=ws.name,
                  target=wt.tree, say=say,
                  fetch=lambda url: strip_html(""), install=False, worktree=wt, test_command="true",
                  pr=pr, approved=approved, resume=resume)


def run_it(engine, lock):
    try:
        return engine.run()
    finally:
        lock.close()


def remote_branches(origin):
    return git(origin, "branch", "--list").split()


# --- approval -----------------------------------------------------------------------------------


def test_yes_pushes_the_branch_and_opens_the_pull_request(tmp_path, repo_dir, scratch, cfg, staff, origin, gh):
    engine, ws, lock = start(tmp_path, repo_dir, scratch, cfg, staff, approved=True)
    assert run_it(engine, lock)
    record = run_record(ws)
    assert "factory/cart" in remote_branches(origin)
    assert git(origin, "rev-parse", "factory/cart") == record["commit"]
    pr = record["pull_request"]
    assert pr["url"] == URL and pr["approved_by"] == "--yes" and pr["base"] == "main"
    argv = gh.argv[0]
    assert argv[:3] == ["gh", "pr", "create"] and argv[argv.index("--head") + 1] == "factory/cart"
    assert argv[argv.index("--base") + 1] == "main" and argv[argv.index("--title") + 1] == "Add a cart"


def test_without_approval_the_run_waits_and_pushes_nothing(tmp_path, repo_dir, scratch, cfg, staff, origin, gh):
    engine, ws, lock = start(tmp_path, repo_dir, scratch, cfg, staff)  # no --yes
    assert not run_it(engine, lock)
    record = run_record(ws)
    assert record["status"] == "awaiting" and record["detail"] == "approval"
    assert record["commit"] and "pull_request" not in record  # committed, not pushed
    assert remote_branches(origin) == [] and gh.argv == []


def test_the_waiting_message_gives_the_command_that_approves(tmp_path, repo_dir, scratch, cfg, staff, origin, gh):
    said = []
    ws, lock = open_workspace(scratch / "shop", "cart", rebuild=False)
    wt = repo.open_worktree(repo_dir, "cart", None, ws)
    engine = build(tmp_path, ws, wt, cfg, staff, change_agents(dev_writes=WRITES), say=said.append)
    run_it(engine, lock)
    text = "\n".join(said)
    assert "Waiting for approval" in text and "is committed but not pushed" in text
    assert f"To approve, run: factory approve --name cart --repo {repo_dir.resolve()}" in text
    assert run_record(ws)["awaiting"]["point"] == "push"


def test_without_pr_nothing_is_pushed_even_with_yes(tmp_path, repo_dir, scratch, cfg, staff, origin, gh):
    ws, lock = open_workspace(scratch / "shop", "cart", rebuild=False)
    wt = repo.open_worktree(repo_dir, "cart", None, ws)
    engine = build(tmp_path, ws, wt, cfg, staff, change_agents(dev_writes=WRITES), approved=True, pr=False)
    assert run_it(engine, lock)
    assert remote_branches(origin) == [] and gh.argv == []
    assert "pull_request" not in run_record(ws)


# --- the pull request ------------------------------------------------------------------------------


def test_the_body_holds_the_criteria_the_gates_and_the_usage(tmp_path, repo_dir, scratch, cfg, staff, origin, gh):
    engine, ws, lock = start(tmp_path, repo_dir, scratch, cfg, staff, approved=True)
    run_it(engine, lock)
    argv = gh.argv[0]
    body = argv[argv.index("--body") + 1]
    assert "- AC1: Adding an item shows it in the cart" in body and "- AC2: The cart total is shown" in body
    assert "repo_tests_pass" in body and "review_approves" in body
    assert "Factory run: " + run_record(ws)["run_id"] in body and "Approved by: --yes" in body
    assert "Usage" in body


def test_a_failed_push_keeps_the_commit_and_resume_does_not_commit_again(tmp_path, repo_dir, scratch, cfg, staff, gh):
    git(repo_dir, "remote", "add", "origin", str(tmp_path / "no-such-remote.git"))  # the push fails
    engine, ws, lock = start(tmp_path, repo_dir, scratch, cfg, staff, approved=True)
    assert not run_it(engine, lock)
    failed = run_record(ws)
    assert failed["status"] == "stopped" and "git push -u origin factory/cart" in failed["detail"]
    assert "--resume --name cart" in failed["detail"] and failed["commit"] and gh.argv == []
    bare = tmp_path / "fixed.git"  # the owner fixes the remote, then resumes
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(repo_dir, "remote", "set-url", "origin", str(bare))
    ws, lock = open_workspace(scratch / "shop", "cart", rebuild=True)
    agents = Agents({})  # a resumed run asks no agent for anything
    engine = build(tmp_path, ws, repo.reuse_worktree(failed), cfg, staff, agents, approved=True, resume=True)
    assert run_it(engine, lock)
    record = run_record(ws)
    assert agents.requests == [] and record["commit"] == failed["commit"]
    assert git(repo_dir, "rev-list", "--count", "main..factory/cart") == "1"
    assert record["pull_request"]["url"] == URL and "factory/cart" in git(bare, "branch", "--list")


def test_an_approved_awaiting_run_resumes_to_the_pull_request(tmp_path, repo_dir, scratch, cfg, staff, origin, gh):
    engine, ws, lock = start(tmp_path, repo_dir, scratch, cfg, staff)
    run_it(engine, lock)
    waiting = run_record(ws)
    ws, lock = open_workspace(scratch / "shop", "cart", rebuild=True)
    agents = Agents({})
    engine = build(tmp_path, ws, repo.reuse_worktree(waiting), cfg, staff, agents, approved=True, resume=True)
    assert run_it(engine, lock)
    assert agents.requests == [] and run_record(ws)["status"] == "accepted"
    assert run_record(ws)["pull_request"]["approved_by"] == "--yes"
    assert git(origin, "rev-parse", "factory/cart") == waiting["commit"]


def test_a_person_approves_the_push_with_factory_approve(tmp_path, repo_dir, scratch, cfg, staff, origin, gh):
    import json
    engine, ws, lock = start(tmp_path, repo_dir, scratch, cfg, staff)
    run_it(engine, lock)
    waiting = run_record(ws)
    assert waiting["awaiting"]["point"] == "push" and waiting["pr"] is True
    waiting["approvals"] = [*waiting.get("approvals", []),
                            {"point": "push", "decision": "accept", "by": "person", "at": "now"}]
    (ws / ".factory" / "run.json").write_text(json.dumps(waiting))  # what `factory approve` writes
    ws, lock = open_workspace(scratch / "shop", "cart", rebuild=True)
    engine = build(tmp_path, ws, repo.reuse_worktree(waiting), cfg, staff, Agents({}), resume=True)
    assert run_it(engine, lock)
    assert run_record(ws)["pull_request"]["approved_by"] == "person"
    assert "Approved by: person" in gh.argv[0][gh.argv[0].index("--body") + 1]


def test_a_missing_gh_stops_the_run_with_a_clear_message(tmp_path, repo_dir, scratch, cfg, staff, origin, monkeypatch):
    real = repo.run

    def no_gh(argv, **kw):
        if argv[0] == "gh":
            raise FileNotFoundError("gh")
        return real(argv, **kw)

    monkeypatch.setattr(repo, "run", no_gh)
    engine, ws, lock = start(tmp_path, repo_dir, scratch, cfg, staff, approved=True)
    assert not run_it(engine, lock)
    assert "gh is not installed" in run_record(ws)["detail"]
    assert run_record(ws)["commit"]  # the commit stays


def test_a_gh_failure_stops_the_run_and_shows_its_message(tmp_path, repo_dir, scratch, cfg, staff, origin, gh):
    gh.fail = "HTTP 401: bad credentials"
    engine, ws, lock = start(tmp_path, repo_dir, scratch, cfg, staff, approved=True)
    assert not run_it(engine, lock)
    detail = run_record(ws)["detail"]
    assert "gh pr create" in detail and "bad credentials" in detail and "pull_request" not in run_record(ws)


def test_a_detached_checkout_has_no_base_and_stops_before_the_push(tmp_path, repo_dir, scratch, cfg, staff, origin, gh):
    git(repo_dir, "checkout", "-q", "--detach")
    engine, ws, lock = start(tmp_path, repo_dir, scratch, cfg, staff, approved=True)
    assert not run_it(engine, lock)
    assert "no branch" in run_record(ws)["detail"] and remote_branches(origin) == []


# --- secrets ------------------------------------------------------------------------------------


def test_a_secret_in_the_change_stops_the_run_before_the_commit(tmp_path, repo_dir, scratch, cfg, staff, origin, gh):
    key = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    agents = change_agents(dev_writes={**WRITES, "config.py": f"TOKEN = '{key}'\n"})
    engine, ws, lock = start(tmp_path, repo_dir, scratch, cfg, staff, approved=True, agents=agents)
    assert not run_it(engine, lock)
    record = run_record(ws)
    assert "look like secrets" in record["detail"] and "config.py:1" in record["detail"]
    assert key not in record["detail"] and record["commit"] is None
    assert git(repo_dir, "rev-list", "--count", "main..factory/cart") == "0"
    assert remote_branches(origin) == []


def test_a_secret_in_an_untouched_file_does_not_stop_the_run(tmp_path, repo_dir, scratch, cfg, staff, origin, gh):
    key = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    (repo_dir / "old.py").write_text(f"TOKEN = '{key}'\n")  # already in the repo, not part of the change
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-q", "-m", "an old secret")
    engine, ws, lock = start(tmp_path, repo_dir, scratch, cfg, staff, approved=True)
    assert run_it(engine, lock)


# --- the command line ---------------------------------------------------------------------------


def test_pr_is_refused_for_a_runbook_that_changes_no_repo(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "APPS_DIR", tmp_path / "apps")
    assert cli.main(["run", "--runbook", "research", "--pr", "--brief", "x", "--yes"]) == 2
    assert "--pr only applies to a runbook that changes a git repo" in capsys.readouterr().err


def test_pr_is_refused_for_impact_analysis(monkeypatch, tmp_path, capsys, repo_dir):
    monkeypatch.setattr(cli, "APPS_DIR", tmp_path / "apps")
    assert cli.main(["run", "--runbook", "impact-analysis", "--repo", str(repo_dir), "--pr",
                     "--brief", "x", "--yes"]) == 2
    assert "--pr only applies" in capsys.readouterr().err


def test_the_engine_refuses_pr_without_a_worktree(tmp_path, cfg, staff):
    from test_engine import make_engine
    from factory.runbook import catalog, resolve

    plan = resolve(catalog()["research"], {}, staff, cfg)
    with pytest.raises(ConfigError, match="needs --repo before --pr"):
        make_engine(plan, cfg, tmp_path, Agents({}), pr=True)


def test_a_repo_with_no_origin_stops_before_the_approval(tmp_path, repo_dir, scratch, cfg, staff, gh):
    engine, ws, lock = start(tmp_path, repo_dir, scratch, cfg, staff, approved=True)
    assert not run_it(engine, lock)
    assert "no remote named origin" in run_record(ws)["detail"] and gh.argv == []
    assert run_record(ws)["commit"]


def test_the_remote_url_hides_a_password(repo_dir):
    git(repo_dir, "remote", "add", "origin", "https://user:s3cret@github.com/acme/shop.git")
    assert repo.remote_url(repo_dir) == "https://github.com/acme/shop.git"
