"""The factory's command line.

  factory runbooks                                  list the runbooks and their traits
  factory plan  RUNBOOK [--trait T=yes|no ...]      show the steps and agents a run would use
  factory runs  [--json]                            sum up the runs in apps/ (read only)
  factory check [--worker W ...]                    send one short prompt to each model
  factory run   --brief "..." [options]             do the work

`run` picks the runbook itself (a triage agent reads the brief) unless --runbook is given, and
shows you its choice and the plan before any work starts. Examples:

  factory run --brief "a kanban board where I can add tickets and columns"
  factory run --brief "a shared shopping list for my family" --trait backend=yes
  factory run --runbook web-app --trait frontend=no --trait backend=yes --brief "a URL shortener API"
  factory run --brief "which vector database fits a 10M-document RAG system?"
  factory run --runbook code-audit --target apps/kanban-react/web --brief "is this ready to ship?"
  factory run --runbook change-request --repo ~/code/shop --test-command "npm test" --brief "add a cart"
  factory run --runbook bug-fix --repo ~/code/shop --test-command "npm test" --brief "the cart total ignores removed items"

Work happens in apps/<name>/ (--name, or chosen from the brief), with the run log, the
artifacts and every agent's log in apps/<name>/.factory/.

A change request or a bug fix works on a git repo (--repo) in its own worktree, under FACTORY_SCRATCH
(default ~/factory-scratch): <scratch>/<repo>/<feature>/tree, on the branch factory/<feature>.
The original checkout is not touched. The run ends with one commit on that branch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .a2a_transport import Json
from .config import ConfigError, FactoryError, load_config
from .engine import Engine, name_from, new_run_id, open_workspace, triage
from .fleet import Fleet
from .paths import APPS_DIR, LLM_CONFIG, ROOT, RUNBOOKS_DIR
from . import check, repo, runs, tasks
from .check import check_all
from .runbook import (LoopDef, Runbook, StaffDef, catalog, describe_catalog, load_staff, mermaid,
                      outline, resolve)
from .schemas import NO_RUNBOOK, render

AGENTS_FILE = ROOT / "agents.yaml"
YES, NO = {"yes", "y", "true", "1", "on"}, {"no", "n", "false", "0", "off"}


def parse_traits(pairs: list[str]) -> dict[str, bool]:
    out = {}
    for p in pairs:
        key, sep, value = p.partition("=")
        if not sep or value.lower() not in YES | NO:
            raise ConfigError(f"--trait {p!r}: use NAME=yes or NAME=no")
        out[key.strip()] = value.lower() in YES
    return out


def parse_staff(pairs: list[str]) -> dict[str, StaffDef]:
    out = {}
    for p in pairs:
        role, sep, rest = p.partition("=")
        if not sep or not rest:
            raise ConfigError(f"--staff {p!r}: use ROLE=WORKER or ROLE=WORKER:MODEL")
        worker, _, model = rest.partition(":")
        out[role.strip()] = StaffDef(worker=worker, model=model or None)
    return out


def _ask_yes(question: str) -> bool:
    return input(question).strip().lower() in ("", *YES)


def cmd_runbooks(args) -> int:
    print(describe_catalog(catalog(args.runbooks)))
    return 0


def cmd_runs(args) -> int:
    records, unreadable = runs.load_runs(APPS_DIR)
    summary = runs.summarize(records)
    print(json.dumps(summary, indent=2) if args.json else runs.render(summary, unreadable))
    return 0


def _plan(args, cfg, rb: Runbook, traits: dict[str, bool]):
    staff = load_staff(args.agents, cfg, {**rb.staff, **parse_staff(args.staff)})
    return resolve(rb, traits, staff, cfg)


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    results = check_all(cfg, ROOT, args.worker or None)
    print(check.render(results))
    return 0 if results and all(r.ok for r in results) else 1


def cmd_plan(args) -> int:
    cfg = load_config(args.config)
    books = catalog(args.runbooks)
    if args.runbook not in books:
        raise ConfigError(f"no runbook {args.runbook!r}; runbooks are {', '.join(books)}")
    plan = _plan(args, cfg, books[args.runbook], parse_traits(args.trait))
    if args.mermaid:
        print(mermaid(plan))
    else:
        print(outline(plan))
        print(f"\nTask list (a run writes it to .factory/TASKS.md):\n{tasks.plan_text(plan)}")
    return 0


def _shown(path: Path) -> str:
    """A folder as the console shows it: relative to the project when it is inside it."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _changes_repo(rb: Runbook) -> bool:
    return any(s.writes == "target" for n in rb.steps for s in (n.steps if isinstance(n, LoopDef) else [n]))


def _check_repo_args(args, rb: Runbook) -> None:
    """--repo is for a runbook that works on git repos. A runbook that changes one repo (change-request,
    bug-fix) takes one --repo and a test command. A runbook that only reads repos (impact-analysis)
    takes one or more --repo and no other repo flag."""
    repos = args.repo or []
    given = [flag for flag, value in (("--repo", repos), ("--base", args.base),
                                      ("--test-command", args.test_command)) if value]
    if rb.needs_repos:
        if not repos:
            raise ConfigError(f"runbook {rb.name} reads git repos: pass --repo PATH once for each repo")
        if args.base or args.test_command or args.target:
            raise ConfigError(f"--base, --test-command and --target do not apply to {rb.name}, "
                              "which only reads the repos")
    elif _changes_repo(rb):
        if len(repos) != 1:
            raise ConfigError(f"runbook {rb.name} changes one git repo: pass --repo PATH once, the repo folder")
        if not args.test_command:
            raise ConfigError(f"runbook {rb.name} needs --test-command, the command that runs the repo's "
                              'tests, for example --test-command "npm test"')
        if args.target:
            raise ConfigError("--target is not used with --repo: the repo is the target")
    elif given:
        raise ConfigError(f"{', '.join(given)} only applies to a runbook that changes a git repo "
                          "(change-request, bug-fix) or reads git repos (impact-analysis)")
    if args.pr and not _changes_repo(rb):
        raise ConfigError(f"--pr only applies to a runbook that changes a git repo (change-request, bug-fix), "
                          f"not {rb.name}")


def _read_repos(args) -> list[Path]:
    """The repos a read-only runbook reads in place. Each --repo is the top folder of a git repo that has
    a commit. Each folder name is a service name, so two repos cannot share one."""
    paths = []
    for given in args.repo:
        path = Path(given).expanduser().resolve()
        if repo.toplevel(path) != path:
            raise ConfigError(f"--repo {given!r} must be the top folder of a git repo, not a folder inside one")
        repo.resolve_base(path, None)  # raises when the repo has no commit yet
        paths.append(path)
    names = [p.name for p in paths]
    if dup := sorted({n for n in names if names.count(n) > 1}):
        raise ConfigError(f"two --repo folders have the name {', '.join(dup)}; each folder name is a "
                          "service name, so give one of them another folder name")
    return paths


def _workdir(args, rb: Runbook | None = None) -> tuple[Path, Path | None]:
    """Where a run's folders go: apps/ for a plain run and for a read-only runbook, <scratch>/<repo>/ for a
    runbook that changes a git repo (P10). Also the repo's top folder, or None."""
    if not args.repo or (rb is not None and not _changes_repo(rb)):
        return APPS_DIR, None
    top = repo.toplevel(args.repo[0])
    workdir = repo.scratch_root() / top.name
    repo.check_scratch(top, workdir)
    return workdir, top


def _find_home(args) -> Path:
    """The folder of the run --resume continues. A change lives in its repo's scratch folder, and a plain run
    or a read-only runbook lives in apps/. The first folder that holds the run wins."""
    if not args.name:
        raise ConfigError("--resume needs --name, the folder of the run to continue")
    homes = [*([_workdir(args)[0]] if args.repo else []), APPS_DIR]
    for home in homes:
        if (home / args.name / ".factory" / "run.json").exists():
            return home
    raise ConfigError(f"nothing to resume: {args.name} has no run record in "
                      f"{', '.join(_shown(h) for h in homes)}")


def resume_record(args, workdir: Path) -> Json:
    """The run record of <workdir>/<name>, for --resume. Refuses a run that is accepted or missing."""
    if not args.name:
        raise ConfigError("--resume needs --name, the folder of the run to continue")
    path = workdir / args.name / ".factory" / "run.json"
    if not path.exists():
        raise ConfigError(f"nothing to resume: {workdir.name}/{args.name} has no run record")
    record = json.loads(path.read_text())
    if record.get("status") == "accepted":
        raise ConfigError(f"{workdir.name}/{args.name} is already accepted; there is nothing to resume")
    return record


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    books = catalog(args.runbooks)
    interactive = not args.yes
    if args.resume:
        return _resume(args, cfg, books, interactive)
    traits = parse_traits(args.trait)
    target = str(Path(args.target).resolve()) if args.target else None

    run_id = new_run_id()  # one ID for triage and the run, in every request and agent log line
    print(f"Run {run_id}")
    triage_usage: list = []  # the LLM calls of triage, logged in the run
    if args.runbook:
        if args.runbook not in books:
            raise ConfigError(f"no runbook {args.runbook!r}; runbooks are {', '.join(books)}")
        rb, name = books[args.runbook], args.name or name_from(args.brief)
    else:
        staff = load_staff(args.agents, cfg, parse_staff(args.staff))
        print("Choosing a runbook for the brief...")
        with Fleet(staff, ["triage"], APPS_DIR / ".factory" / "triage") as fleet:
            choice = triage(fleet.send, args.brief, books, target=target, on_input=None, cwd=ROOT,
                            usage=triage_usage, run_id=run_id)
        print(f"\n{render('triage', choice)}\n")
        if choice["runbook"] == NO_RUNBOOK:
            print("There is no runbook for this request, so the factory did not start. "
                  f"Run {run_id} made no workspace.\n"
                  f"Runbooks: {', '.join(books)}. See `factory runbooks`.")
            return 1
        rb = books[choice["runbook"]]
        traits = {**choice["traits"], **traits}  # traits given on the command line win
        name = args.name or choice["name"]
        target = target or (str(Path(choice["target"]).resolve()) if choice.get("target") else None)

    _check_repo_args(args, rb)
    workdir, top = _workdir(args, rb)
    repos = _read_repos(args) if rb.needs_repos else []
    plan = _plan(args, cfg, rb, traits)
    print(outline(plan))
    if interactive and not _ask_yes("\nRun this plan? [Y/n] "):
        print("Not started. Change it with --runbook, --trait or --staff.")
        return 1
    return _run_plan(args, cfg, plan, workdir=workdir, top=top, name=name, brief=args.brief,
                     interactive=interactive, run_id=run_id, earlier_usage=triage_usage,
                     target=target, resume=False, rebuild=args.rebuild, record=None, repos=repos)


def _run_plan(args, cfg, plan, *, workdir: Path, top: Path | None, name: str, brief: str,
              interactive: bool, run_id: str, earlier_usage: list | None, target: str | None,
              resume: bool, rebuild: bool, record: Json | None, repos: list[Path]) -> int:
    """Lock <workdir>/<name>, make the repo worktree when the plan changes a repo, and run the engine.
    `repos` are the repos a read-only runbook reads in place; no worktree is made for them."""
    ws, lock = open_workspace(workdir, name, rebuild)  # the lock comes first: one run per feature
    try:
        print(f"\nWorking in {_shown(ws)}")
        worktree = None
        if top is not None:
            worktree = (repo.reuse_worktree(record) if record
                        else repo.open_worktree(top, name, args.base, ws))
            target = str(worktree.tree)  # the worktree is the target: the agents change only that
        with Fleet(plan.staff, plan.roles(), ws / ".factory" / "agents", args.config) as fleet:
            print("A2A agents:", *(f"  {line}" for line in fleet.describe()), sep="\n")
            engine = Engine(plan, cfg, ws, fleet.send, brief=brief, name=name,
                            target=Path(target) if target else None, interactive=interactive,
                            earlier_usage=earlier_usage, run_id=run_id, resume=resume,
                            send_fallback=fleet.send_fallback, worktree=worktree,
                            test_command=args.test_command, repos=repos, pr=args.pr,
                            approved=bool(args.yes))
            return 0 if engine.run() else 1
    finally:
        lock.close()


def _resume(args, cfg, books, interactive: bool) -> int:
    """Continue the run's folder from its run record: items it accepted are kept, the rest run again."""
    home = _find_home(args)
    record = resume_record(args, home)
    if record["runbook"] not in books:
        raise ConfigError(f"the run used runbook {record['runbook']!r}, which is not in the catalog")
    rb = books[record["runbook"]]
    _check_repo_args(args, rb)
    workdir, top = _workdir(args, rb)
    if workdir != home:
        raise ConfigError(f"run {args.name} is in {_shown(home)}, but runbook {rb.name} works in {_shown(workdir)}")
    repos = _read_repos(args) if rb.needs_repos else []
    traits = {k: bool(v) for k, v in record.get("traits", {}).items()}
    plan = _plan(args, cfg, rb, traits)
    print(f"Resuming run {record['run_id']} in {_shown(workdir / args.name)}")
    print(outline(plan))
    if interactive and not _ask_yes("\nResume this plan? [Y/n] "):
        print("Not started.")
        return 1
    return _run_plan(args, cfg, plan, workdir=workdir, top=top, name=args.name, brief=record["brief"],
                     interactive=interactive, run_id=record["run_id"], earlier_usage=None,
                     target=record.get("target"), resume=True, rebuild=True,
                     record=record if top is not None else None,  # accepted runs were refused above
                     repos=repos)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="factory", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=LLM_CONFIG, help="llm.yaml")
    ap.add_argument("--agents", type=Path, default=AGENTS_FILE, help="agents.yaml: who plays each role")
    ap.add_argument("--runbooks", type=Path, default=RUNBOOKS_DIR, help="directory of runbook YAML files")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("runbooks", help="list the runbooks").set_defaults(fn=cmd_runbooks)

    p = sub.add_parser("runs", help="sum up the runs in apps/ (read only)")
    p.add_argument("--json", action="store_true", help="print the totals as JSON")
    p.set_defaults(fn=cmd_runs)

    p = sub.add_parser("check", help="send one short prompt to each model and report")
    p.add_argument("--worker", action="append", default=[], metavar="NAME",
                   help="check only this worker (repeatable)")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("plan", help="show what a run would do, without running it")
    p.add_argument("runbook")
    p.add_argument("--trait", action="append", default=[], metavar="NAME=yes|no")
    p.add_argument("--staff", action="append", default=[], metavar="ROLE=WORKER[:MODEL]")
    p.add_argument("--mermaid", action="store_true", help="print a Mermaid flowchart")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("run", help="do the work")
    p.add_argument("--brief", required=True, help="what you want, in your own words")
    p.add_argument("--runbook", help="skip triage and use this runbook")
    p.add_argument("--trait", action="append", default=[], metavar="NAME=yes|no")
    p.add_argument("--staff", action="append", default=[], metavar="ROLE=WORKER[:MODEL]",
                   help="who plays a role in this run")
    p.add_argument("--name", help="work in apps/<name> (default: chosen from the brief)")
    p.add_argument("--target", help="existing code to work on (code-audit)")
    p.add_argument("--repo", action="append", default=[], metavar="PATH",
                   help="git repo: change-request and bug-fix change one (work happens in a worktree of it); "
                        "impact-analysis reads each one you give, in place (repeatable)")
    p.add_argument("--base", help="commit the new branch starts from (change-request, bug-fix; default: HEAD)")
    p.add_argument("--test-command", metavar="CMD",
                   help='the command that runs the repo\'s tests, e.g. "npm test" (change-request, bug-fix)')
    p.add_argument("--pr", action="store_true",
                   help="after the commit, push the branch to origin and open a pull request (change-request, "
                        "bug-fix). Needs approval: a person answers the prompt, or --yes approves it")
    p.add_argument("--rebuild", action="store_true", help="redo work that was already accepted")
    p.add_argument("--resume", action="store_true",
                   help="continue the run in --name: keep what it accepted, redo the rest")
    p.add_argument("--yes", action="store_true",
                   help="no questions or approvals: agents decide, and every checkpoint is accepted")
    p.set_defaults(fn=cmd_run)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except FactoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
