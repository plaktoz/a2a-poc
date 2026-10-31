"""Work on a real git repo (P10): a worktree on its own branch, a write guard and one commit.

The factory never touches the checkout it was pointed at. It makes a git worktree of the repo on a
new branch `factory/<feature>`, outside the repo, under the scratch root:

    <scratch>/<repo>/<feature>/
        .factory/      run.json, artifacts.json, lock, agent logs
        tree/          the worktree

Git finds what an agent changed (`git status`), so a big repo is never read into memory. Nothing
is pushed or merged: the run ends with one commit on the branch, for a person to review.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError, FactoryError
from .process import run, tail

SCRATCH_DEFAULT = "~/factory-scratch"
GIT_TIMEOUT = 120
_CLEAN = object()  # a path that is not in a dirty-file map is unchanged from HEAD


class GitError(FactoryError):
    """A git command the run depends on failed."""


@dataclass(frozen=True)
class Worktree:
    repo: Path  # the top of the original checkout
    base: str  # the commit the branch starts from (a full SHA, so it stays true when HEAD moves)
    branch: str
    tree: Path

    @property
    def fields(self) -> dict[str, str | None]:
        """What the run record keeps; `commit` is filled in when the change is committed."""
        return {"repo": str(self.repo), "base": self.base, "branch": self.branch,
                "tree": str(self.tree), "commit": None}


@dataclass
class TreeState:
    """What a step may change in a worktree, taken before and after it."""

    files: dict[str, bytes | None]  # dirty path -> content (None: deleted)
    head: str
    gitfile: bytes | None  # the worktree's `.git` file, which points at the real git directory


def scratch_root() -> Path:
    """FACTORY_SCRATCH, or ~/factory-scratch."""
    return Path(os.environ.get("FACTORY_SCRATCH") or SCRATCH_DEFAULT).expanduser()


def branch_name(feature: str) -> str:
    return f"factory/{feature}"


def _git(where: Path, *args: str, check: bool = True, read_only: bool = False):
    """Run git in `where`. read_only skips git's optional index refresh, so a status check never writes."""
    argv = ["git", *(["--no-optional-locks"] if read_only else []), "-C", str(where), *args]
    try:
        r = run(argv, cwd=where, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        raise GitError("git is not installed") from None
    if check and r.returncode != 0:
        raise GitError(f"`git {' '.join(args)}` failed in {where}: {tail(r, 1000).strip()}")
    return r


def toplevel(repo: str | Path) -> Path:
    """The top folder of the git repo at `repo`, or a ConfigError when it is not one."""
    path = Path(repo).expanduser()
    if not path.is_dir():
        raise ConfigError(f"--repo {str(repo)!r} is not a folder")
    try:
        r = _git(path, "rev-parse", "--show-toplevel", check=False)
    except GitError as e:
        raise ConfigError(str(e)) from None
    if r.returncode != 0 or not r.stdout.strip():
        raise ConfigError(f"--repo {str(repo)!r} is not a git repository")
    return Path(r.stdout.strip()).resolve()


def resolve_base(top: Path, base: str | None) -> str:
    """The commit SHA of `base` (default HEAD)."""
    ref = base or "HEAD"
    r = _git(top, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if r.returncode != 0 or not r.stdout.strip():
        raise ConfigError(f"--base {ref!r} is not a commit in {top}"
                          + (" (the repo has no commits yet)" if ref == "HEAD" else ""))
    return r.stdout.strip()


def branch_exists(top: Path, branch: str) -> bool:
    return _git(top, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0


def is_dirty(top: Path) -> bool:
    return bool(_git(top, "status", "--porcelain", read_only=True).stdout.strip())


def dirty_warning(top: Path) -> str:
    return (f"Warning: {top} has uncommitted changes. They are not in the worktree, which starts "
            "from the committed code. Leave the checkout alone while the run goes on.")


def create_worktree(top: Path, tree: Path, feature: str, base: str) -> Worktree:
    """Add `tree` as a worktree on the new branch factory/<feature>. Makes nothing if the branch exists."""
    branch = branch_name(feature)
    if branch_exists(top, branch):
        raise ConfigError(f"branch {branch} already exists in {top}. Choose another --name; "
                          "no worktree was made.")
    r = _git(top, "worktree", "add", "-b", branch, str(tree), base, check=False)
    if r.returncode != 0:
        raise ConfigError(f"cannot make the worktree {tree}: {tail(r, 1000).strip()}")
    return Worktree(top, base, branch, tree.resolve())


def check_scratch(top: Path, scratch: Path) -> None:
    """The run's folders must not sit inside the repo: they would show up as changes in the checkout."""
    if scratch.resolve().is_relative_to(top):
        raise ConfigError(f"the scratch folder {scratch} is inside the repo {top}. Set FACTORY_SCRATCH "
                          "to a folder outside it.")


def open_worktree(repo: str | Path, feature: str, base: str | None, workspace: Path,
                  say=print) -> Worktree:
    """Check the repo, warn if it is dirty, and make <workspace>/tree on factory/<feature>."""
    top = toplevel(repo)
    sha = resolve_base(top, base)
    if is_dirty(top):
        say(dirty_warning(top))
    return create_worktree(top, workspace / "tree", feature, sha)


def reuse_worktree(record: dict) -> Worktree:
    """The worktree of an earlier attempt, from its run record, for --resume. Refuses one that is gone."""
    tree, branch = Path(record["tree"]), record["branch"]
    top = Path(record["repo"])
    if not tree.is_dir() or not branch_exists(top, branch):
        raise ConfigError(f"nothing to resume: the worktree {tree} or branch {branch} is gone")
    return Worktree(top, record["base"], branch, tree.resolve())


# --- finding and undoing changes -------------------------------------------------------------


def status_paths(tree: Path) -> list[str]:
    """The paths `git status` lists: modified, added, deleted, renamed and untracked files."""
    out = _git(tree, "status", "--porcelain", "-z", "--untracked-files=all", read_only=True).stdout
    parts, paths, i = out.split("\0"), [], 0
    while i < len(parts):
        entry, i = parts[i], i + 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" and i < len(parts):  # the next entry is the path it came from
            paths.append(parts[i])
            i += 1
    return paths


def dirty_files(tree: Path) -> dict[str, bytes | None]:
    """Every file that differs from HEAD, with its content now (None if deleted)."""
    out: dict[str, bytes | None] = {}
    for rel in status_paths(tree):
        p = tree / rel
        if p.is_symlink():
            out[rel] = os.readlink(p).encode()
        elif p.is_file():
            out[rel] = p.read_bytes()
        else:
            out[rel] = None
    return out


def snapshot_tree(tree: Path) -> TreeState:
    gitfile = tree / ".git"
    return TreeState(dirty_files(tree), _git(tree, "rev-parse", "HEAD").stdout.strip(),
                     gitfile.read_bytes() if gitfile.is_file() else None)


def revert_git(tree: Path, before: TreeState) -> list[str]:
    """Undo a change to the worktree's git plumbing. Returns what was found and put back."""
    found = []
    gitfile = tree / ".git"
    now = gitfile.read_bytes() if gitfile.is_file() else None
    if now != before.gitfile:
        if gitfile.is_dir():  # an agent replaced the link with a folder of its own
            shutil.rmtree(gitfile)
        if before.gitfile is None:
            gitfile.unlink(missing_ok=True)
        else:
            gitfile.write_bytes(before.gitfile)
        found.append(".git")
    head = _git(tree, "rev-parse", "HEAD").stdout.strip()
    if head != before.head:
        _git(tree, "reset", "--mixed", "--quiet", before.head)  # keeps the files the agent wrote
        found.append("a commit made in the worktree (HEAD moved)")
    return found


def touched(before: dict[str, bytes | None], after: dict[str, bytes | None]) -> set[str]:
    return {p for p in before.keys() | after.keys() if before.get(p, _CLEAN) != after.get(p, _CLEAN)}


def revert_files(where: Path, before: dict[str, bytes | None], paths: set[str]) -> None:
    """Put each path back as it was in `before`; a path that was clean goes back to HEAD."""
    for rel in sorted(paths):
        p = where / rel
        if rel in before:
            if before[rel] is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(before[rel])
        elif _git(where, "ls-files", "--error-unmatch", "--", rel, check=False).returncode == 0:
            _git(where, "checkout", "HEAD", "--", rel)
        else:
            p.unlink(missing_ok=True)


def diff_text(tree: Path, limit: int = 20000) -> str:
    """The change so far: the diff of tracked files against HEAD, and the names of new files."""
    diff = _git(tree, "diff", "HEAD", "--no-color", read_only=True, check=False).stdout
    new = _git(tree, "ls-files", "--others", "--exclude-standard", check=False).stdout.split()
    text = diff[:limit] + ("\n... (diff cut)" if len(diff) > limit else "")
    if new:
        text += "\nNew files (read them in the working directory): " + ", ".join(new)
    return text.strip() or "(no changes yet)"


# --- commit ----------------------------------------------------------------------------------


def commit(tree: Path, title: str, criteria: list[str], run_id: str) -> str | None:
    """Stage everything and make one plain, unsigned commit. Returns its SHA, or None if nothing changed."""
    _git(tree, "add", "-A")
    staged = _git(tree, "diff", "--cached", "--quiet", check=False).returncode  # 1: something is staged
    if staged == 0:
        return None
    body = ("Acceptance criteria:\n" + "\n".join(f"- {c}" for c in criteria) + "\n\n") if criteria else ""
    r = _git(tree, "-c", "commit.gpgsign=false", "commit", "-m", title,
             "-m", f"{body}Factory run: {run_id}", check=False)
    if r.returncode != 0:
        raise GitError(f"git commit failed: {tail(r, 1500).strip()}")
    return _git(tree, "rev-parse", "HEAD").stdout.strip()


def diff_command(wt: Worktree) -> str:
    return f"git -C {wt.repo} diff {wt.base}...{wt.branch}"


def cleanup_command(wt: Worktree) -> str:
    return f"git -C {wt.repo} worktree remove {wt.tree}"


# --- pull request (P10 slice 2) ---------------------------------------------------------------


def has_remote(tree: Path, name: str = "origin") -> bool:
    return _git(tree, "remote", "get-url", name, check=False).returncode == 0


def remote_url(tree: Path, name: str = "origin") -> str:
    """The fetch URL of the remote, with any user and password removed, for display."""
    url = _git(tree, "remote", "get-url", name).stdout.strip()
    return re.sub(r"^(\w+://)[^/@]+@", r"\1", url)


def push_branch(tree: Path, branch: str) -> str:
    """`git push -u origin <branch>`. Returns the remote's answer. Raises GitError when the push fails."""
    r = _git(tree, "push", "-u", "origin", branch, check=False)
    if r.returncode != 0:
        raise GitError(f"`git push -u origin {branch}` failed: {tail(r, 1500).strip()}")
    return tail(r, 2000).strip()


def create_pr(tree: Path, branch: str, base: str, title: str, body: str) -> str:
    """`gh pr create` from the worktree. Returns the PR URL that gh prints. Raises GitError when it fails."""
    argv = ["gh", "pr", "create", "--head", branch, "--base", base, "--title", title, "--body", body]
    try:
        r = run(argv, cwd=tree, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        raise GitError("gh is not installed. Install the GitHub CLI and run `gh auth login`.") from None
    if r.returncode != 0:
        raise GitError(f"`gh pr create` failed: {tail(r, 1500).strip()}")
    urls = [line.strip() for line in r.stdout.splitlines() if line.strip().startswith("https://")]
    return urls[-1] if urls else r.stdout.strip()


def current_branch(top: Path) -> str | None:
    """The branch the original checkout is on, or None when HEAD is detached."""
    r = _git(top, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    name = r.stdout.strip()
    return name if r.returncode == 0 and name else None
