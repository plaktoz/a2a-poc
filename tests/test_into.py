"""`--into REPO`: a run builds in <repo>/<name>/ instead of apps/. The repo must be on a branch, and a
default branch needs --allow-main. Real git repos in tmp_path; no LLM."""

import argparse

import pytest

from factory import ConfigError, cli, repo
from factory.engine import open_workspace
from factory.runbook import catalog
from test_repo import git


def args(**over):
    base = dict(into=None, allow_main=False, repo=[], name="hcc", cmd="run")
    return argparse.Namespace(**{**base, **over})


def make_repo(path, branch):
    path.mkdir()
    git(path, "init", "-q", "-b", branch)
    (path / "README.md").write_text("x\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    return path.resolve()


APP = catalog()["app-build"]


def test_no_into_leaves_the_workdir_alone():
    assert cli._into(args()) is None
    assert cli._workdir(args(), APP) == (cli.APPS_DIR, None)


def test_a_feature_branch_is_accepted_and_is_the_workdir(tmp_path):
    top = make_repo(tmp_path / "hcc", "feat-x")
    assert cli._into(args(into=str(top))) == top
    assert cli._workdir(args(into=str(top)), APP) == (top, None)


@pytest.mark.parametrize("branch", ["main", "master"])
def test_a_default_branch_needs_allow_main(tmp_path, branch):
    top = make_repo(tmp_path / "hcc", branch)
    with pytest.raises(ConfigError, match=f"is on {branch}, the default branch.*--allow-main"):
        cli._into(args(into=str(top)))
    assert cli._into(args(into=str(top), allow_main=True)) == top


def test_the_branch_origin_calls_default_needs_allow_main_too(tmp_path):
    top = make_repo(tmp_path / "hcc", "trunk")
    git(top, "update-ref", "refs/remotes/origin/trunk", "HEAD")
    git(top, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert "trunk" in repo.default_branches(top)
    with pytest.raises(ConfigError, match="default branch"):
        cli._into(args(into=str(top)))


def test_a_detached_head_is_refused(tmp_path):
    top = make_repo(tmp_path / "hcc", "feat-x")
    git(top, "checkout", "-q", "--detach")
    with pytest.raises(ConfigError, match="detached HEAD"):
        cli._into(args(into=str(top)))


def test_a_folder_inside_a_repo_or_outside_one_is_refused(tmp_path):
    top = make_repo(tmp_path / "hcc", "feat-x")
    (top / "sub").mkdir()
    with pytest.raises(ConfigError, match="top folder"):
        cli._into(args(into=str(top / "sub")))
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ConfigError, match="not a git repository"):
        cli._into(args(into=str(plain)))


def test_into_does_not_go_with_repo_or_with_runbooks_that_work_on_a_repo(tmp_path):
    top = make_repo(tmp_path / "hcc", "feat-x")
    with pytest.raises(cli.UsageError, match="do not go together"):
        cli._into(args(into=str(top), repo=[str(top)]))
    with pytest.raises(cli.UsageError, match="builds new code"):
        cli._workdir(args(into=str(top)), catalog()["change-request"])


def test_resume_looks_in_the_into_repo(tmp_path):
    top = make_repo(tmp_path / "hcc", "feat-x")
    run = top / "hcc" / ".factory"
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}")
    assert cli._find_home(args(into=str(top))) == top


def test_the_run_folder_keeps_its_records_out_of_the_repos_changes(tmp_path):
    top = make_repo(tmp_path / "hcc", "feat-x")
    ws, lock = open_workspace(top, "hcc", False)
    try:
        (ws / ".factory" / "run.json").write_text("{}")
        (ws / "web").mkdir()
        (ws / "web" / "a.txt").write_text("code")
        assert git(top, "status", "--porcelain", "--untracked-files=all") == "?? hcc/web/a.txt"
    finally:
        lock.close()
