"""Work on a real git repo (P10): worktrees, the write guard, the test gate and the commit (no LLM).

The repos are real git repos in tmp_path. The agents are the fakes of test_engine.py: they write
files in the worktree they are sent to, so the engine's own checks run on real git state.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from factory import ConfigError, repo
from factory.engine import Engine, open_workspace
from factory.gates import GATES, Failure, GateCtx
from factory.runbook import catalog, resolve
from test_engine import Agents, strip_html, write_files
from test_usage import cfg, staff  # noqa: F401  (fixtures)

GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
SPEC = {"title": "Add a cart", "goal": "g", "acceptance_criteria": [
    {"id": "AC1", "text": "Adding an item shows it in the cart"},
    {"id": "AC2", "text": "The cart total is shown"}]}
APPROVE = lambda req: {"verdict": "approve", "issues": [], "strengths": []}  # noqa: E731
TESTS_PASS = f'{sys.executable} -c "print(\'ok\')"'
TESTS_FAIL = f'{sys.executable} -c "import sys; print(\'AC1 failed: cart is empty\'); sys.exit(3)"'


def git(where, *args):
    r = subprocess.run(["git", *GIT_ID, "-C", str(where), *args], capture_output=True, text=True, check=True)
    return r.stdout.strip()


@pytest.fixture
def repo_dir(tmp_path):
    """A small git repo with one commit, in <tmp>/shop."""
    path = tmp_path / "shop"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    write_files(path, {"app.py": "def cart():\n    return []\n", "README.md": "shop\n"})
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    return path


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    monkeypatch.setenv("FACTORY_SCRATCH", str(root))
    return root


def change_plan(cfg, staff):
    return resolve(catalog()["change-request"], {}, staff, cfg)


def change_agents(*, dev_writes=None, verdicts=None):
    """A developer that writes `dev_writes` into its worktree, and a reviewer with `verdicts`."""
    def developer(req):
        if dev_writes:
            write_files(req["cwd"], dev_writes)
        return {"summary": "done", "test_change": ""}

    return Agents({
        ("analyst", "spec"): [SPEC],
        ("developer", "change"): developer,
        ("reviewer", "verify"): (lambda req: verdicts.pop(0)) if verdicts else APPROVE,
    })


def make_change(tmp_path, repo_dir, scratch, cfg, staff, agents, *, feature="cart", test_command=TESTS_PASS,
                base=None, say=lambda m: None):
    """Start a change-request run the way the CLI does: lock, worktree, engine."""
    ws, lock = open_workspace(scratch / repo_dir.name, feature, rebuild=False)
    wt = repo.open_worktree(repo_dir, feature, base, ws, say=say)
    engine = Engine(change_plan(cfg, staff), cfg, ws, agents, brief="add a cart", name=feature,
                    target=wt.tree, interactive=False, say=say, fetch=lambda url: strip_html(""),
                    install=False, worktree=wt, test_command=test_command)
    return engine, ws, lock


def run_record(ws):
    return json.loads((ws / ".factory" / "run.json").read_text())


# --- worktree -------------------------------------------------------------------------------


def test_worktree_on_the_new_branch(repo_dir, scratch, cfg, staff, tmp_path):
    engine, ws, lock = make_change(tmp_path, repo_dir, scratch, cfg, staff, change_agents())
    try:
        tree = scratch / "shop" / "cart" / "tree"
        assert tree.is_dir() and (tree / "app.py").exists()
        assert git(tree, "rev-parse", "--abbrev-ref", "HEAD") == "factory/cart"
        assert git(repo_dir, "rev-parse", "--verify", "factory/cart")
        assert engine.worktree.tree == tree.resolve()
    finally:
        lock.close()


def test_the_base_defaults_to_head_and_can_be_overridden(repo_dir, scratch, cfg, staff, tmp_path):
    first = git(repo_dir, "rev-parse", "HEAD")
    write_files(repo_dir, {"app.py": "def cart():\n    return ['x']\n"})
    git(repo_dir, "commit", "-q", "-am", "second")
    _, _, lock = make_change(tmp_path, repo_dir, scratch, cfg, staff, change_agents(), feature="head")
    lock.close()
    assert git(scratch / "shop" / "head" / "tree", "rev-parse", "HEAD") == git(repo_dir, "rev-parse", "HEAD")
    _, _, lock = make_change(tmp_path, repo_dir, scratch, cfg, staff, change_agents(), feature="old",
                             base=first)
    lock.close()
    assert git(scratch / "shop" / "old" / "tree", "rev-parse", "HEAD") == first


def test_a_branch_that_exists_is_a_config_error_and_no_worktree_is_made(repo_dir, scratch, cfg, staff, tmp_path):
    git(repo_dir, "branch", "factory/cart")
    ws, lock = open_workspace(scratch / "shop", "cart", rebuild=False)
    try:
        with pytest.raises(ConfigError, match="Choose another --name"):
            repo.open_worktree(repo_dir, "cart", None, ws)
        assert not (ws / "tree").exists()
        assert str(scratch) not in git(repo_dir, "worktree", "list")
    finally:
        lock.close()


def test_a_non_git_folder_is_a_config_error(tmp_path, scratch):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ConfigError, match="not a git repository"):
        repo.toplevel(plain)


def test_a_scratch_folder_inside_the_repo_is_refused(repo_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_SCRATCH", str(repo_dir / "inside"))
    with pytest.raises(ConfigError, match="inside the repo"):
        repo.check_scratch(repo_dir, repo_dir / "inside" / "shop")


def test_the_original_checkout_is_unchanged(repo_dir, scratch, cfg, staff, tmp_path):
    before = git(repo_dir, "status", "--porcelain", "--untracked-files=all")
    head = git(repo_dir, "rev-parse", "HEAD")
    engine, ws, lock = make_change(tmp_path, repo_dir, scratch, cfg, staff,
                                   change_agents(dev_writes={"app.py": "def cart():\n    return ['a']\n"}))
    try:
        assert engine.run()
    finally:
        lock.close()
    assert git(repo_dir, "status", "--porcelain", "--untracked-files=all") == before
    assert git(repo_dir, "rev-parse", "HEAD") == head
    assert (repo_dir / "app.py").read_text() == "def cart():\n    return []\n"


def test_two_features_of_one_repo_run_together(repo_dir, scratch, cfg, staff, tmp_path):
    a, ws_a, lock_a = make_change(tmp_path, repo_dir, scratch, cfg, staff, change_agents(), feature="cart")
    b, ws_b, lock_b = make_change(tmp_path, repo_dir, scratch, cfg, staff, change_agents(), feature="search")
    try:
        assert ws_a != ws_b
        assert (ws_a / ".factory" / "lock").exists() and (ws_b / ".factory" / "lock").exists()
        git(repo_dir, "rev-parse", "--verify", "factory/cart")
        git(repo_dir, "rev-parse", "--verify", "factory/search")
    finally:
        lock_a.close()
        lock_b.close()


def test_the_same_feature_twice_fails_on_the_lock(repo_dir, scratch, cfg, staff, tmp_path):
    _, _, lock = make_change(tmp_path, repo_dir, scratch, cfg, staff, change_agents(), feature="cart")
    try:
        with pytest.raises(ConfigError, match="already being worked on"):
            open_workspace(scratch / "shop", "cart", rebuild=False)
    finally:
        lock.close()


def test_a_dirty_repo_prints_a_warning(repo_dir, scratch, tmp_path):
    write_files(repo_dir, {"notes.txt": "not committed\n"})
    said = []
    ws, lock = open_workspace(scratch / "shop", "cart", rebuild=False)
    try:
        repo.open_worktree(repo_dir, "cart", None, ws, say=said.append)
    finally:
        lock.close()
    assert any("uncommitted changes" in line for line in said)


def test_a_clean_repo_prints_no_warning(repo_dir, scratch, tmp_path):
    said = []
    ws, lock = open_workspace(scratch / "shop", "cart", rebuild=False)
    try:
        repo.open_worktree(repo_dir, "cart", None, ws, say=said.append)
    finally:
        lock.close()
    assert not said


# --- write guard -----------------------------------------------------------------------------


def first_attempt_only(misbehave, then):
    """A developer reply that misbehaves on its first attempt, then does the honest change."""
    calls = []

    def developer(req):
        calls.append(req)
        return misbehave(req) if len(calls) == 1 else then(req)

    return developer


HONEST = {"app.py": "def cart():\n    return ['a']\n"}


def honest(req):
    write_files(req["cwd"], HONEST)
    return {"summary": "done", "test_change": ""}


def test_a_change_to_git_is_reverted_and_a_source_change_is_kept(repo_dir, scratch, cfg, staff, tmp_path):
    def tamper(req):
        write_files(req["cwd"], HONEST)
        gitlink = Path(req["cwd"]) / ".git"
        gitlink.unlink()
        gitlink.mkdir()  # the worktree's git link replaced by a folder of its own
        return {"summary": "done", "test_change": ""}

    agents = Agents({
        ("analyst", "spec"): [SPEC],
        ("developer", "change"): first_attempt_only(tamper, honest),
        ("reviewer", "verify"): APPROVE,
    })
    engine, ws, lock = make_change(tmp_path, repo_dir, scratch, cfg, staff, agents)
    try:
        assert engine.run()
    finally:
        lock.close()
    tree = scratch / "shop" / "cart" / "tree"
    assert (tree / ".git").is_file()  # the worktree's git link is back
    assert (tree / "app.py").read_text() == HONEST["app.py"]
    events = run_record(ws)["events"]
    assert any(e["status"] == "rejected" and ".git" in e["detail"] for e in events)


def test_a_write_outside_the_worktree_is_reverted_and_fails_the_step(repo_dir, scratch, cfg, staff, tmp_path):
    outside = repo_dir / "stray.txt"

    def stray(req):
        write_files(req["cwd"], HONEST)
        outside.write_text("written outside the worktree\n")  # in the original checkout
        return {"summary": "done", "test_change": ""}

    agents = Agents({
        ("analyst", "spec"): [SPEC],
        ("developer", "change"): first_attempt_only(stray, honest),
        ("reviewer", "verify"): APPROVE,
    })
    engine, ws, lock = make_change(tmp_path, repo_dir, scratch, cfg, staff, agents)
    try:
        assert engine.run()  # the retry, without the stray write, passes
    finally:
        lock.close()
    assert not outside.exists()  # reverted
    rejected = [e for e in run_record(ws)["events"] if e["status"] == "rejected"]
    assert any("outside your worktree" in e["detail"] for e in rejected)


# --- test gate -------------------------------------------------------------------------------


def gate_ctx(tree, command):
    return GateCtx(app_dir=tree, cwd=tree, dirs=[], templates={}, artifacts={}, fetch=lambda u: None,
                   tree=tree, test_command=command)


def test_the_tests_gate_fails_and_shows_the_command_output(tmp_path):
    with pytest.raises(Failure, match="AC1 failed: cart is empty") as info:
        GATES["repo_tests_pass"](gate_ctx(tmp_path, TESTS_FAIL), {})
    assert info.value.level == "round"
    assert "exit code 3" in info.value.message


def test_the_tests_gate_passes_when_the_command_passes(tmp_path):
    GATES["repo_tests_pass"](gate_ctx(tmp_path, TESTS_PASS), {})


def test_the_tests_gate_needs_a_command(tmp_path):
    with pytest.raises(Failure, match="--test-command") as info:
        GATES["repo_tests_pass"](gate_ctx(tmp_path, None), {})
    assert info.value.level == "run"


def test_a_failing_test_command_stops_the_loop_round_with_its_output(repo_dir, scratch, cfg, staff, tmp_path):
    agents = change_agents(dev_writes={"app.py": "def cart():\n    return ['a']\n"})
    engine, ws, lock = make_change(tmp_path, repo_dir, scratch, cfg, staff, agents, test_command=TESTS_FAIL)
    try:
        assert not engine.run()
    finally:
        lock.close()
    stopped = run_record(ws)
    assert stopped["status"] == "stopped"
    assert "AC1 failed: cart is empty" in stopped["detail"]


# --- commit ----------------------------------------------------------------------------------


def test_the_commit_is_one_commit_with_the_run_id_in_its_body(repo_dir, scratch, cfg, staff, tmp_path):
    agents = change_agents(dev_writes={"app.py": "def cart():\n    return ['a']\n"})
    engine, ws, lock = make_change(tmp_path, repo_dir, scratch, cfg, staff, agents)
    try:
        assert engine.run()
    finally:
        lock.close()
    tree = scratch / "shop" / "cart" / "tree"
    record = run_record(ws)
    assert record["status"] == "accepted"
    assert git(tree, "rev-list", "--count", "main..factory/cart") == "1"
    body = git(tree, "log", "-1", "--format=%B", "factory/cart")
    assert record["run_id"] in body and "AC1" not in body and "Adding an item shows it" in body
    assert git(tree, "log", "-1", "--format=%s", "factory/cart") == "Add a cart"
    assert record["commit"] == git(tree, "rev-parse", "factory/cart")
    assert record["branch"] == "factory/cart" and record["base"] == git(repo_dir, "rev-parse", "main")
    assert record["repo"] == str(repo_dir.resolve()) and record["tree"] == str(tree.resolve())


def test_the_commit_names_the_run_in_the_log_of_the_change(repo_dir, scratch, cfg, staff, tmp_path):
    agents = change_agents(dev_writes={"app.py": "def cart():\n    return ['a']\n"})
    engine, ws, lock = make_change(tmp_path, repo_dir, scratch, cfg, staff, agents)
    try:
        assert engine.run()
    finally:
        lock.close()
    assert git(scratch / "shop" / "cart" / "tree", "log", "-1", "--format=%B", "factory/cart").count("Factory run:") == 1


def test_an_accepted_change_with_no_file_changes_is_stopped_not_committed(repo_dir, scratch, cfg, staff, tmp_path):
    agents = change_agents()  # the developer changes nothing
    engine, ws, lock = make_change(tmp_path, repo_dir, scratch, cfg, staff, agents)
    try:
        assert not engine.run()
    finally:
        lock.close()
    assert run_record(ws)["status"] == "stopped"
    assert run_record(ws)["commit"] is None


def test_nothing_is_pushed_or_merged(repo_dir, scratch, cfg, staff, tmp_path):
    agents = change_agents(dev_writes={"app.py": "def cart():\n    return ['a']\n"})
    engine, ws, lock = make_change(tmp_path, repo_dir, scratch, cfg, staff, agents)
    try:
        assert engine.run()
    finally:
        lock.close()
    assert git(repo_dir, "rev-parse", "--verify", "factory/cart")
    assert git(repo_dir, "rev-parse", "main") == git(repo_dir, "rev-parse", "HEAD")  # main did not move


# --- the CLI ---------------------------------------------------------------------------------


def test_the_cli_needs_a_test_command_for_the_change_request(monkeypatch, tmp_path, capsys, repo_dir, scratch):
    from factory import cli

    monkeypatch.setattr(cli, "APPS_DIR", tmp_path / "apps")
    assert cli.main(["run", "--runbook", "change-request", "--repo", str(repo_dir), "--brief", "x"]) == 2
    assert "--test-command" in capsys.readouterr().err


def test_the_cli_needs_a_repo_for_the_change_request(monkeypatch, tmp_path, capsys):
    from factory import cli

    monkeypatch.setattr(cli, "APPS_DIR", tmp_path / "apps")
    assert cli.main(["run", "--runbook", "change-request", "--test-command", "true", "--brief", "x"]) == 2
    assert "--repo" in capsys.readouterr().err


def test_repo_flags_are_refused_for_other_runbooks(monkeypatch, tmp_path, capsys):
    from factory import cli

    monkeypatch.setattr(cli, "APPS_DIR", tmp_path / "apps")
    assert cli.main(["run", "--runbook", "research", "--test-command", "true", "--brief", "x", "--yes"]) == 2
    assert "only applies to a runbook that changes a git repo" in capsys.readouterr().err


def test_the_cli_takes_the_folder_from_the_repo_name(repo_dir, scratch):
    from factory import cli

    assert cli._workdir(type("A", (), {"repo": [str(repo_dir)]})()) == (scratch / "shop", repo_dir.resolve())
