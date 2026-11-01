"""The factory's command line.

  factory runbooks                                  list the runbooks and their traits
  factory plan  RUNBOOK [--trait T=yes|no ...]      show the steps and agents a run would use
  factory runs  [--json]                            sum up the runs in apps/ (read only)
  factory check [--worker W ...]                    send one short prompt to each model
  factory run   --brief "..." [options]             do the work
  factory approve --name NAME [--changes "..."]     answer a run that waits at `awaiting: approval`

`run` picks the runbook itself (a triage agent reads the brief) unless --runbook is given, and
prints its choice and the plan. No command asks a question. A run that needs a person's review
stops at `awaiting: approval` and prints the `factory approve` command; --yes approves every
review in advance. Examples:

  factory run --brief "a kanban board where I can add tickets and columns"
  factory run --brief "a shared shopping list for my family" --trait backend=yes
  factory run --runbook app-build --trait frontend=no --trait backend=yes --brief "a URL shortener API"
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
import time
from pathlib import Path

from .a2a_transport import Json
from .config import ConfigError, FactoryError, load_config
from .engine import Engine, name_from, new_run_id, open_workspace, triage
from .fleet import Fleet
from .out import UsageError
from .paths import APPS_DIR, LLM_CONFIG, ROOT, RUNBOOKS_DIR
from . import check, out, repo, runs, tasks
from .check import check_all
from .runbook import (LoopDef, Runbook, StaffDef, catalog, load_staff, mermaid,
                      outline, resolve, resolve_inputs)
from .schemas import NO_RUNBOOK, render

AGENTS_FILE = ROOT / "agents.yaml"


YES, NO = {"yes", "y", "true", "1", "on"}, {"no", "n", "false", "0", "off"}


def parse_traits(pairs: list[str]) -> dict[str, bool]:
    out = {}
    for p in pairs:
        key, sep, value = p.partition("=")
        if not sep or value.lower() not in YES | NO:
            raise UsageError(f"--trait {p!r}: use NAME=yes or NAME=no")
        out[key.strip()] = value.lower() in YES
    return out


def parse_inputs(pairs: list[str]) -> dict[str, str]:
    """--input NAME=VALUE, once for each input. The runbook says which names it has."""
    out = {}
    for p in pairs:
        key, sep, value = p.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise UsageError(f"--input {p!r}: use NAME=VALUE")
        out[key.strip()] = value.strip()
    return out


def read_interface(path: Path | None) -> str:
    """The interface details the user supplied in a file. They are never part of a runbook."""
    if path is None:
        return ""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise UsageError(f"--interface {path}: cannot read it as text ({e.__class__.__name__})") from None
    if not text.strip():
        raise UsageError(f"--interface {path} is empty")
    return text


def parse_staff(pairs: list[str]) -> dict[str, StaffDef]:
    out = {}
    for p in pairs:
        role, sep, rest = p.partition("=")
        if not sep or not rest:
            raise UsageError(f"--staff {p!r}: use ROLE=WORKER or ROLE=WORKER:MODEL")
        worker, _, model = rest.partition(":")
        out[role.strip()] = StaffDef(worker=worker, model=model or None)
    return out


RUNBOOK_FIELDS = (["name", "title", "traits"], ["description", "use_when", "needs"])


def cmd_home(args) -> int:
    """`factory` with no command: live data first, not help text (PRD FR-27)."""
    home = str(Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else "factory")
    user = str(Path.home())
    if home.startswith(user):
        home = "~" + home[len(user):]
    records, unreadable = runs.load_runs(APPS_DIR)
    books = catalog(args.runbooks)
    rows = [{"name": r["app"], "status": r.get("status", "unknown"), "runbook": r.get("runbook", "unknown"),
             "cost_usd": round((r.get("usage") or {}).get("cost_usd", 0.0), 4)} for r in records]
    data = {"bin": home, "description": "A software factory: agents on A2A do a task from a runbook, and the "
            "engine checks every result.", "runs": rows if rows else "0 found",
            "runbooks": [{"name": rb.name, "title": rb.title} for rb in books.values()]}
    print(out.show(data, args, help=["factory runbooks", "factory run --brief <brief>", "factory runs"]))
    return 0


SKILL = """---
name: factory
description: Run the software factory (runbooks, agents on A2A) to build an app, research a question, audit code, change a repo, fix a bug or analyse the impact of a change. Use when the user asks for that work.
---

# factory

Run `{bin}` with no arguments to see the runs and the runbooks. Every command prints TOON; add `--json` for JSON.

- Start work: `factory run --brief "<what the user wants>"`. Add `--yes` only if the user agreed to approve every review in advance.
- A run that needs a review stops with status `awaiting`. Show the draft to the user, then `factory approve --name <run>` or `--changes "<words>"`.
- A stopped run continues with `factory run --resume --name <run>`.
- Each command ends with `help[]` lines: the likely next commands.
"""


def session_summary(apps: Path) -> Json:
    """A short summary of the runs in apps/ for the start of a session (PRD FR-26): name, status, cost."""
    records, _ = runs.load_runs(apps)
    rows = [{"name": r["app"], "status": r.get("status", "unknown"),
             "cost_usd": round((r.get("usage") or {}).get("cost_usd", 0.0), 4)} for r in records]
    return {"runs": rows if rows else "0 found"}


def cmd_session(args) -> int:
    """Print the summary that the session hook adds to a new session. Writes nothing."""
    data = session_summary(APPS_DIR)
    waiting = [r["name"] for r in data["runs"] if isinstance(data["runs"], list) and r["status"] == "awaiting"]
    print(out.show(data, args, help=[f"factory approve --name {n}" for n in waiting] + ["factory"]))
    return 0


def cmd_setup(args) -> int:
    """Install ambient context into --dir (default: here): a session hook in .claude/settings.json and an
    Agent Skill in .claude/skills/factory/SKILL.md. Each Claude Code session there then starts with the
    runs and their status. Running it again changes nothing (PRD FR-25)."""
    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        raise UsageError(f"--dir {args.dir}: not a directory")
    exe = str(Path(sys.argv[0]).resolve()) if sys.argv and sys.argv[0] else "factory"
    command = f"{exe} session"
    settings_path = root / ".claude" / "settings.json"
    try:
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except ValueError as e:
        raise ConfigError(f"{settings_path} is not valid JSON ({e}); fix it, then run setup again") from None
    hooks = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    known = any(h.get("command") == command for group in hooks for h in group.get("hooks", []))
    if not known:
        hooks.append({"hooks": [{"type": "command", "command": command}]})
    skill_path = root / ".claude" / "skills" / "factory" / "SKILL.md"
    skill = SKILL.format(bin=exe)
    changed = []
    if not known:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        changed.append(str(settings_path))
    if not skill_path.exists() or skill_path.read_text() != skill:
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(skill)
        changed.append(str(skill_path))
    print(out.show({"setup": "changed" if changed else "already done", "dir": str(root), "files": changed},
                   args, help=["factory session", "factory"]))
    return 0


def cmd_runbooks(args) -> int:
    rows = [{"name": rb.name, "title": rb.title, "traits": " ".join(rb.traits) or "none",
             "description": out.clip(rb.description, args.full), "use_when": out.clip(rb.use_when, args.full),
             "needs": ("target" if rb.needs_target else "repos" if rb.needs_repos else "none")}
            for rb in catalog(args.runbooks).values()]
    print(out.show({"total": len(rows), "runbooks": out.choose(rows, *RUNBOOK_FIELDS, args.fields)}, args,
                   help=["factory plan <runbook>", "factory run --runbook <runbook> --brief <brief>",
                         "factory runbooks --fields description,use_when,needs --full"]))
    return 0


def cmd_runs(args) -> int:
    records, unreadable = runs.load_runs(APPS_DIR)
    summary = runs.summarize(records)
    by_status = summary["by_status"]
    nxt = (["factory approve --name <run>"] if by_status.get("awaiting") else []) + \
          (["factory run --resume --name <run>"] if by_status.get("stopped") else [])
    print(out.show(runs.view(summary, unreadable), args, plain=summary,
                   help=nxt + ["factory run --brief <brief>", "factory runbooks"]))
    return 0


def _plan(args, cfg, rb: Runbook, traits: dict[str, bool], inputs: dict[str, str] | None = None,
          require_inputs: bool = True):
    staff = load_staff(args.agents, cfg, {**rb.staff, **parse_staff(args.staff)})
    values = resolve_inputs(rb, inputs or {}, require=require_inputs)
    return resolve(rb, traits, staff, cfg, inputs=values)


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    results = check_all(cfg, ROOT, args.worker or None)
    report = check.report(results, args.full, args.fields)
    ok = bool(results) and all(r.ok for r in results)
    print(out.show(report, args, help=(["factory run --brief <brief>"] if ok else
                                       ["factory check --worker <worker>", "factory check --full"])))
    return 0 if ok else 1


def cmd_plan(args) -> int:
    cfg = load_config(args.config)
    books = catalog(args.runbooks)
    if args.runbook not in books:
        raise UsageError(f"no runbook {args.runbook!r}; runbooks are {', '.join(books)}")
    plan = _plan(args, cfg, books[args.runbook], parse_traits(args.trait), parse_inputs(args.input),
                 require_inputs=False)
    if args.mermaid:
        print(mermaid(plan))
    else:
        print(outline(plan))
        print(f"\nTask list (a run writes it to .factory/TASKS.md):\n{tasks.plan_text(plan)}")
    print("\n" + out.toon({"help": [f"factory run --runbook {args.runbook} --brief <brief>",
                                    f"factory plan {args.runbook} --mermaid"]}))
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
            raise UsageError(f"runbook {rb.name} reads git repos: pass --repo PATH once for each repo")
        if args.base or args.test_command or args.target:
            raise UsageError(f"--base, --test-command and --target do not apply to {rb.name}, "
                              "which only reads the repos")
    elif _changes_repo(rb):
        if len(repos) != 1:
            raise UsageError(f"runbook {rb.name} changes one git repo: pass --repo PATH once, the repo folder")
        if not args.test_command:
            raise UsageError(f"runbook {rb.name} needs --test-command, the command that runs the repo's "
                              'tests, for example --test-command "npm test"')
        if args.target:
            raise UsageError("--target is not used with --repo: the repo is the target")
    elif given:
        raise UsageError(f"{', '.join(given)} only applies to a runbook that changes a git repo "
                          "(change-request, bug-fix) or reads git repos (impact-analysis)")
    if args.pr and not _changes_repo(rb):
        raise UsageError(f"--pr only applies to a runbook that changes a git repo (change-request, bug-fix), "
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
        raise UsageError("--resume needs --name, the folder of the run to continue")
    homes = [*([_workdir(args)[0]] if args.repo else []), APPS_DIR]
    for home in homes:
        if (home / args.name / ".factory" / "run.json").exists():
            return home
    raise ConfigError(f"no run to {'approve' if args.cmd == 'approve' else 'resume'}: {args.name} has no run record in "
                      f"{', '.join(_shown(h) for h in homes)}")


def resume_record(args, workdir: Path) -> Json:
    """The run record of <workdir>/<name>, for --resume. Refuses a run that is accepted or missing."""
    if not args.name:
        raise UsageError("--resume needs --name, the folder of the run to continue")
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
    if args.resume:
        return _resume(args, cfg, books)
    traits = parse_traits(args.trait)
    inputs = parse_inputs(args.input)
    interface = read_interface(args.interface)
    target = str(Path(args.target).resolve()) if args.target else None

    run_id = new_run_id()  # one ID for triage and the run, in every request and agent log line
    print(f"Run {run_id}")
    triage_usage: list = []  # the LLM calls of triage, logged in the run
    if args.runbook:
        if args.runbook not in books:
            raise UsageError(f"no runbook {args.runbook!r}; runbooks are {', '.join(books)}")
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
        inputs = {**{k: ",".join(v) if isinstance(v, list) else str(v) for k, v in choice["inputs"].items()},
                  **inputs}  # so do inputs
        name = args.name or choice["name"]
        target = target or (str(Path(choice["target"]).resolve()) if choice.get("target") else None)

    _check_repo_args(args, rb)
    workdir, top = _workdir(args, rb)
    repos = _read_repos(args) if rb.needs_repos else []
    plan = _plan(args, cfg, rb, traits, inputs)
    print(outline(plan))
    return _run_plan(args, cfg, plan, workdir=workdir, top=top, name=name, brief=args.brief,
                     run_id=run_id, earlier_usage=triage_usage,
                     target=target, resume=False, rebuild=args.rebuild, record=None, repos=repos,
                     interface=interface)


def _next_steps(engine) -> list[str]:
    """The likely next commands after a run (PRD FR-28): fixed flags stay, run values are placeholders."""
    data = engine.log.data
    if data.get("status") == "awaiting":
        point = (data.get("awaiting") or {}).get("point")
        return ["factory approve --name <run>"] + (['factory approve --name <run> --changes "<what to change>"']
                                                    if point != "push" else [])
    if data.get("status") == "accepted":
        return ["factory runs", "factory run --brief <brief>"]
    return ["factory run --resume --name <run>", "factory runs"]


def _run_plan(args, cfg, plan, *, workdir: Path, top: Path | None, name: str, brief: str,
              run_id: str, earlier_usage: list | None, target: str | None,
              resume: bool, rebuild: bool, record: Json | None, repos: list[Path], interface: str = "") -> int:
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
                            target=Path(target) if target else None, wait_at_checkpoints=not args.yes,
                            earlier_usage=earlier_usage, run_id=run_id, resume=resume,
                            send_fallback=fleet.send_fallback, worktree=worktree,
                            test_command=args.test_command, repos=repos, pr=args.pr,
                            approved=bool(args.yes), interface=interface)
            accepted = engine.run()
            print("\n" + out.toon({"help": _next_steps(engine)}))
            return 0 if accepted else 1
    finally:
        lock.close()


def _resume(args, cfg, books) -> int:
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
    if args.input:
        raise UsageError("--input does not apply to --resume: the run keeps the inputs it started with")
    # The inputs and the interface details come from the run record. A new --interface file replaces the old text.
    plan = _plan(args, cfg, rb, traits, record.get("inputs") or {})
    interface = read_interface(args.interface) or record.get("interface") or ""
    print(f"Resuming run {record['run_id']} in {_shown(workdir / args.name)}")
    print(outline(plan))
    return _run_plan(args, cfg, plan, workdir=workdir, top=top, name=args.name, brief=record["brief"],
                     run_id=record["run_id"], earlier_usage=None,
                     target=record.get("target"), resume=True, rebuild=True,
                     record=record if top is not None else None,  # accepted runs were refused above
                     repos=repos, interface=interface)


def cmd_approve(args) -> int:
    """Answer a run that waits at `awaiting: approval`: record the decision, then continue the run.
    A person runs this; the record says `person`. --changes sends a checkpoint's draft back."""
    home = _find_home(args)
    path = home / args.name / ".factory" / "run.json"
    record = json.loads(path.read_text())
    if record.get("status") != "awaiting" or not record.get("awaiting"):
        raise ConfigError(f"{args.name} is {record.get('status')}, not awaiting approval; there is nothing to approve")
    point = record["awaiting"]["point"]
    if args.changes and point == "push":
        raise UsageError("--changes answers a checkpoint; the push takes only an approval")
    decision = {"point": point, "decision": "changes" if args.changes else "accept", "by": "person",
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **({"changes": args.changes} if args.changes else {})}
    record["approvals"] = [*record.get("approvals", []), decision]
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(out.toon({"approved" if not args.changes else "changes_asked": point, "run": args.name,
                    "by": "person"}))
    cont = argparse.Namespace(**{**vars(args), "resume": True, "yes": False, "pr": bool(record.get("pr")),
                                 "test_command": record.get("test_command"), "base": None, "target": None,
                                 "trait": [], "brief": record.get("brief", ""), "runbook": None, "rebuild": False,
                                 "input": [], "interface": None})
    return cmd_run(cont)


class _Parser(argparse.ArgumentParser):
    """An argparse parser whose errors are data on stdout, with exit code 2 (PRD FR-25)."""

    as_json = False  # set by main() from the command line, before parsing

    def error(self, message):
        print(out.error(message, as_json=_Parser.as_json))
        raise SystemExit(2)

    def add_subparsers(self, **kw):
        return super().add_subparsers(parser_class=_Parser, **kw)


def main(argv: list[str] | None = None) -> int:
    ap = _Parser(prog="factory", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=LLM_CONFIG, help="llm.yaml")
    ap.add_argument("--agents", type=Path, default=AGENTS_FILE, help="agents.yaml: who plays each role")
    ap.add_argument("--runbooks", type=Path, default=RUNBOOKS_DIR, help="directory of runbook YAML files")
    sub = ap.add_subparsers(dest="cmd")  # no command: print live data (cmd_home)
    ap.set_defaults(fn=cmd_home)

    def command(name, help, example):
        """A subcommand whose --help ends with one example (PRD FR-29)."""
        return sub.add_parser(name, help=help, description=help, epilog=f"example:\n  {example}",
                              formatter_class=argparse.RawDescriptionHelpFormatter)

    p = command("runbooks", "list the runbooks", "factory runbooks --fields use_when --full")
    out.add_flags(p, fields=True, full=True)
    p.set_defaults(fn=cmd_runbooks)

    p = command("runs", "sum up the runs in apps/ (read only)", "factory runs --json")
    out.add_flags(p)
    p.set_defaults(fn=cmd_runs)

    p = command("check", "send one short prompt to each model and report", "factory check --worker codex-gpt")
    p.add_argument("--worker", action="append", default=[], metavar="NAME",
                   help="check only this worker (repeatable)")
    out.add_flags(p, fields=True, full=True)
    p.set_defaults(fn=cmd_check)

    p = command("plan", "show what a run would do, without running it", "factory plan app-build --trait backend=yes")
    p.add_argument("runbook")
    p.add_argument("--trait", action="append", default=[], metavar="NAME=yes|no")
    p.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--staff", action="append", default=[], metavar="ROLE=WORKER[:MODEL]")
    p.add_argument("--mermaid", action="store_true", help="print a Mermaid flowchart")
    p.set_defaults(fn=cmd_plan)

    p = command("session", "print the runs for the start of a session (used by the hook)", "factory session")
    out.add_flags(p)
    p.set_defaults(fn=cmd_session)

    p = command("setup", "install the session hook and the Agent Skill in a folder", "factory setup --dir ~/code/shop")
    p.add_argument("--dir", default=".", help="the folder whose Claude Code sessions get the hook (default: here)")
    out.add_flags(p)
    p.set_defaults(fn=cmd_setup)

    p = command("run", "do the work", 'factory run --runbook research --brief "which vector database fits a 10M-document RAG system?"')
    p.add_argument("--brief", required=True, help="what you want, in your own words")
    p.add_argument("--runbook", help="skip triage and use this runbook")
    p.add_argument("--trait", action="append", default=[], metavar="NAME=yes|no")
    p.add_argument("--input", action="append", default=[], metavar="NAME=VALUE",
                   help="a value for one of the runbook's inputs (repeatable; see `factory plan <runbook>`)")
    p.add_argument("--interface", type=Path, metavar="FILE",
                   help="a file with interface details (API paths, schemas, ports); read by the agents, never in a runbook")
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
                        "bug-fix). Needs approval: factory approve, or --yes in advance")
    p.add_argument("--rebuild", action="store_true", help="redo work that was already accepted")
    p.add_argument("--resume", action="store_true",
                   help="continue the run in --name: keep what it accepted, redo the rest")
    p.add_argument("--yes", action="store_true",
                   help="approve in advance: every checkpoint and the push (--pr). The record says --yes")
    p.set_defaults(fn=cmd_run)

    p = command("approve", "answer a run that waits at awaiting: approval, and continue it",
                'factory approve --name kanban --changes "add a question on cost"')
    p.add_argument("--name", required=True, help="the run that waits")
    p.add_argument("--repo", action="append", default=[], metavar="PATH",
                   help="the repo of a change-request or bug-fix run (where its scratch folder is)")
    p.add_argument("--changes", metavar="TEXT", help="ask for changes to a checkpoint's draft instead of approving")
    p.add_argument("--staff", action="append", default=[], metavar="ROLE=WORKER[:MODEL]")
    p.set_defaults(fn=cmd_approve)

    _Parser.as_json = "--json" in (sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(argv)  # a bad command or flag exits with 2 (see _Parser.error)
    try:
        return args.fn(args)
    except UsageError as e:  # a bad command: exit 2, as argparse does (PRD FR-25)
        print(out.error(str(e), as_json=_Parser.as_json))
        return 2
    except FactoryError as e:  # the command is well formed but cannot be done: exit 1
        print(out.error(str(e), as_json=_Parser.as_json))
        return 1


if __name__ == "__main__":
    sys.exit(main())
