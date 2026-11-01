"""The bug-fix runbook: a failing test first, then the fix (no LLM, no network).

The repos are real git repos in tmp_path, as in test_repo.py. The test command is a small script
outside the repo: it fails and names the reproduction test until app.py holds the fix.
"""

import shlex
import sys

import pytest

from factory import repo
from factory.engine import Engine, open_workspace
from factory.gates import GATES, Failure
from factory.runbook import LoopDef, catalog, resolve
from test_engine import Agents, strip_html, write_files
from test_repo import APPROVE, TESTS_PASS, gate_ctx, git, repo_dir, run_record, scratch  # noqa: F401
from test_usage import cfg, staff  # noqa: F401  (fixtures)

TEST_NAME = "tests/test_cart.py::test_total_ignores_removed_items"
BUG_SPEC = {"title": "Fix the cart total", "goal": "Remove an item, then read the total: it still counts the item.",
            "acceptance_criteria": [
                {"id": "AC1", "text": "The bug no longer occurs: the total ignores a removed item"},
                {"id": "AC2", "text": "Adding an item still adds it to the total"}],
            "assumptions": ["cart() never drops removed items"]}
FIXED = {"app.py": "def cart(removed=()):\n    return [i for i in [] if i not in removed]\n"}
REPRO_TEST = {"tests/test_cart.py": "def test_total_ignores_removed_items():\n    assert False\n"}


@pytest.fixture
def suite(tmp_path):
    """A test command that fails, naming TEST_NAME, until app.py has the fix."""
    script = tmp_path / "suite.py"
    script.write_text(
        "import pathlib, sys\n"
        "if 'removed' not in pathlib.Path('app.py').read_text():\n"
        f"    print('FAILED {TEST_NAME} - AssertionError')\n"
        "    sys.exit(1)\n"
        "print('2 passed')\n")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


def failing_for_another_reason():
    return f'{sys.executable} -c "import sys; print(\'FAILED tests/test_other.py::test_db\'); sys.exit(1)"'


def bug_agents(*, tester=None, developer=None, verdicts=None):
    def write_test(req):
        write_files(req["cwd"], REPRO_TEST)
        return {"test_name": TEST_NAME, "summary": "fails: a removed item is still counted"}

    def fix(req):
        write_files(req["cwd"], FIXED)
        return {"summary": "drop removed items", "test_change": ""}

    return Agents({
        ("analyst", "spec"): [BUG_SPEC],
        ("tester", "reproduce"): tester or write_test,
        ("developer", "fix"): developer or fix,
        ("reviewer", "verify"): (lambda req: verdicts.pop(0)) if verdicts else APPROVE,
    })


def make_fix(repo_dir, scratch, cfg, staff, agents, test_command, feature="total"):
    ws, lock = open_workspace(scratch / repo_dir.name, feature, rebuild=False)
    wt = repo.open_worktree(repo_dir, feature, None, ws, say=lambda m: None)
    plan = resolve(catalog()["bug-fix"], {}, staff, cfg)
    engine = Engine(plan, cfg, ws, agents, brief="the cart total counts removed items", name=feature,
                    target=wt.tree, say=lambda m: None, fetch=lambda url: strip_html(""),
                    install=False, worktree=wt, test_command=test_command)
    return engine, ws, lock


# --- the gate --------------------------------------------------------------------------------


def test_the_repro_gate_fails_when_every_test_passes(tmp_path):
    with pytest.raises(Failure, match="the bug is not reproduced: every test passes") as info:
        GATES["repro_fails"](gate_ctx(tmp_path, TESTS_PASS), {"test_name": TEST_NAME})
    assert info.value.level == "step"


def test_the_repro_gate_fails_when_the_output_does_not_name_the_test(tmp_path):
    with pytest.raises(Failure, match="does not name your test") as info:
        GATES["repro_fails"](gate_ctx(tmp_path, failing_for_another_reason()), {"test_name": TEST_NAME})
    assert info.value.level == "step"
    assert "test_other.py::test_db" in info.value.message  # the output is shown


def test_the_repro_gate_passes_when_the_named_test_fails(tmp_path, suite):
    (tmp_path / "app.py").write_text("def cart():\n    return []\n")
    GATES["repro_fails"](gate_ctx(tmp_path, suite), {"test_name": TEST_NAME})


def test_the_repro_gate_needs_a_command(tmp_path):
    with pytest.raises(Failure, match="--test-command") as info:
        GATES["repro_fails"](gate_ctx(tmp_path, None), {"test_name": TEST_NAME})
    assert info.value.level == "run"


def test_a_test_command_that_cannot_run_stops_the_run(tmp_path):
    with pytest.raises(Failure, match="cannot run") as info:
        GATES["repro_fails"](gate_ctx(tmp_path, "no-such-tool-xyz test"), {"test_name": TEST_NAME})
    assert info.value.level == "run"


# --- a run -----------------------------------------------------------------------------------


def test_a_bug_fix_run_makes_one_commit_and_leaves_the_checkout_alone(repo_dir, scratch, cfg, staff, suite):
    before = git(repo_dir, "status", "--porcelain", "--untracked-files=all")
    head = git(repo_dir, "rev-parse", "HEAD")
    agents = bug_agents()
    engine, ws, lock = make_fix(repo_dir, scratch, cfg, staff, agents, suite)
    try:
        assert engine.run()
    finally:
        lock.close()
    tree = scratch / "shop" / "total" / "tree"
    assert run_record(ws)["status"] == "accepted"
    assert git(tree, "rev-list", "--count", "main..factory/total") == "1"
    assert git(tree, "log", "-1", "--format=%s", "factory/total") == "Fix the cart total"
    changed = set(git(tree, "diff", "--name-only", "main", "factory/total").split())
    assert changed == {"app.py", "tests/test_cart.py"}  # the fix and the reproduction test
    assert git(repo_dir, "status", "--porcelain", "--untracked-files=all") == before
    assert git(repo_dir, "rev-parse", "HEAD") == head
    # The developer and the reviewer saw the reproduction.
    sent = {(role, r["task"]): r for role, r in agents.requests}
    assert TEST_NAME in str(sent[("developer", "fix")]) and TEST_NAME in str(sent[("reviewer", "verify")])


def test_a_reproduction_whose_tests_pass_is_retried_then_the_run_stops(repo_dir, scratch, cfg, staff):
    calls = []

    def tester(req):
        calls.append(req)
        return {"test_name": TEST_NAME, "summary": ""}  # writes no failing test

    engine, ws, lock = make_fix(repo_dir, scratch, cfg, staff, bug_agents(tester=tester), TESTS_PASS)
    try:
        assert not engine.run()
    finally:
        lock.close()
    reproduce = resolve(catalog()["bug-fix"], {}, staff, cfg).nodes[1]
    assert len(calls) == 1 + reproduce.retries  # the step's own retries, then the run stops
    assert "the bug is not reproduced" in str(calls[-1])  # the retry is told why the first attempt failed
    record = run_record(ws)
    assert record["status"] == "stopped" and record["commit"] is None
    assert "step reproduce failed" in record["detail"] and "the bug is not reproduced" in record["detail"]
    assert not any(e["step"] == "fix" for e in record["events"])  # no fix without a reproduction


def test_a_fix_that_leaves_the_bug_fails_the_round_with_the_test_output(repo_dir, scratch, cfg, staff, suite):
    def no_fix(req):
        write_files(req["cwd"], {"app.py": "def cart():\n    return []  # looked at it\n"})
        return {"summary": "", "test_change": ""}

    engine, ws, lock = make_fix(repo_dir, scratch, cfg, staff, bug_agents(developer=no_fix), suite)
    try:
        assert not engine.run()
    finally:
        lock.close()
    record = run_record(ws)
    assert record["status"] == "stopped" and TEST_NAME in record["detail"]


# --- the runbook and the CLI -----------------------------------------------------------------


def test_bug_fix_resolves_as_a_plan(cfg, staff):
    plan = resolve(catalog()["bug-fix"], {}, staff, cfg)
    assert plan.runbook.needs_target
    [spec, reproduce, loop] = plan.nodes
    assert (spec.id, reproduce.id) == ("spec", "reproduce")
    assert reproduce.output == "repro" and reproduce.gates == ["secrets_clean", "repro_fails"]
    assert isinstance(loop, LoopDef) and loop.gates == ["repo_tests_pass"]
    assert [s.id for s in loop.steps] == ["fix", "verify"]
    assert all(s.cwd == "target" for s in [spec, reproduce, *loop.steps])


def test_triage_sends_bugs_to_bug_fix_and_features_to_change_request():
    books = catalog()
    assert "bug" in books["bug-fix"].use_when
    assert "bug-fix" in books["change-request"].use_when
    assert "fix a bug" not in books["change-request"].use_when


@pytest.mark.parametrize("flags, missing", [
    (["--test-command", "true"], "--repo"),
    (["--repo", "REPO"], "--test-command"),
])
def test_the_cli_needs_a_repo_and_a_test_command_for_bug_fix(monkeypatch, tmp_path, capsys, repo_dir, scratch,
                                                             flags, missing):
    from factory import cli

    monkeypatch.setattr(cli, "APPS_DIR", tmp_path / "apps")
    flags = [str(repo_dir) if f == "REPO" else f for f in flags]
    assert cli.main(["run", "--runbook", "bug-fix", *flags, "--brief", "x"]) == 2
    assert missing in capsys.readouterr().err
