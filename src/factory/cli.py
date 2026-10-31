"""The factory's command line.

  factory runbooks                                  list the runbooks and their traits
  factory plan  RUNBOOK [--trait T=yes|no ...]      show the steps and agents a run would use
  factory run   --brief "..." [options]             do the work

`run` picks the runbook itself (a triage agent reads the brief) unless --runbook is given, and
shows you its choice and the plan before any work starts. Examples:

  factory run --brief "a kanban board where I can add tickets and columns"
  factory run --brief "a shared shopping list for my family" --trait backend=yes
  factory run --runbook web-app --trait frontend=no --trait backend=yes --brief "a URL shortener API"
  factory run --brief "which vector database fits a 10M-document RAG system?"
  factory run --runbook code-audit --target apps/kanban-react/web --brief "is this ready to ship?"

Work happens in apps/<name>/ (--name, or chosen from the brief), with the run log, the
artifacts and every agent's log in apps/<name>/.factory/.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, FactoryError, load_config
from .engine import Engine, name_from, open_workspace, triage
from .fleet import Fleet
from .paths import APPS_DIR, LLM_CONFIG, ROOT, RUNBOOKS_DIR
from .runbook import (Runbook, StaffDef, catalog, describe_catalog, load_staff, mermaid,
                      outline, resolve)
from .schemas import render

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


def _plan(args, cfg, rb: Runbook, traits: dict[str, bool]):
    staff = load_staff(args.agents, cfg, {**rb.staff, **parse_staff(args.staff)})
    return resolve(rb, traits, staff, cfg)


def cmd_plan(args) -> int:
    cfg = load_config(args.config)
    books = catalog(args.runbooks)
    if args.runbook not in books:
        raise ConfigError(f"no runbook {args.runbook!r}; runbooks are {', '.join(books)}")
    plan = _plan(args, cfg, books[args.runbook], parse_traits(args.trait))
    print(mermaid(plan) if args.mermaid else outline(plan))
    return 0


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    books = catalog(args.runbooks)
    traits = parse_traits(args.trait)
    interactive = not args.yes
    target = str(Path(args.target).resolve()) if args.target else None

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
                            usage=triage_usage)
        print(f"\n{render('triage', choice)}\n")
        rb = books[choice["runbook"]]
        traits = {**choice["traits"], **traits}  # traits given on the command line win
        name = args.name or choice["name"]
        target = target or (str(Path(choice["target"]).resolve()) if choice.get("target") else None)

    plan = _plan(args, cfg, rb, traits)
    print(outline(plan))
    if interactive and not _ask_yes("\nRun this plan? [Y/n] "):
        print("Not started. Change it with --runbook, --trait or --staff.")
        return 1

    ws, lock = open_workspace(APPS_DIR, name, args.rebuild)
    try:
        print(f"\nWorking in {ws.relative_to(ROOT)}")
        with Fleet(plan.staff, plan.roles(), ws / ".factory" / "agents", args.config) as fleet:
            print("A2A agents:", *(f"  {line}" for line in fleet.describe()), sep="\n")
            engine = Engine(plan, cfg, ws, fleet.send, brief=args.brief, name=name,
                            target=Path(target) if target else None, interactive=interactive,
                            earlier_usage=triage_usage)
            return 0 if engine.run() else 1
    finally:
        lock.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="factory", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=LLM_CONFIG, help="llm.yaml")
    ap.add_argument("--agents", type=Path, default=AGENTS_FILE, help="agents.yaml: who plays each role")
    ap.add_argument("--runbooks", type=Path, default=RUNBOOKS_DIR, help="directory of runbook YAML files")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("runbooks", help="list the runbooks").set_defaults(fn=cmd_runbooks)

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
    p.add_argument("--rebuild", action="store_true", help="redo work that was already accepted")
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
