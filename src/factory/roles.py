"""Agent roles: the kinds of agent a runbook can staff a step with.

A role says what an agent is for, what it may do and how it behaves, whatever the task. A
runbook step gives the task. agents.yaml (or the runbook) says which worker and model play
each role, so the same role can run on Claude in one runbook and on Codex in another.

`runs` names the commands the role may run. `test` and `build` come from the scaffold's
templates/<name>.yaml; `search` (the factory's own web search) and `curl` are always available.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .invoke import Tools

SEARCH = f"{sys.executable} -m factory.search"
COMMON_COMMANDS = {"search": SEARCH, "curl": "curl"}
NETWORK_COMMANDS = {"search", "curl"}


@dataclass(frozen=True)
class Role:
    name: str
    title: str
    description: str
    prompt: str
    edit: bool = False
    web: bool = False
    runs: tuple[str, ...] = ()
    asks: bool = False  # may pause its task to ask the user questions

    def tools(self, commands: dict[str, str]) -> Tools:
        """The tools for this role in a directory whose commands are `commands`."""
        available = {**COMMON_COMMANDS, **commands}
        runs = [r for r in self.runs if r in available]
        return Tools(edit=self.edit, web=self.web, bash=tuple(dict.fromkeys(available[r] for r in runs)),
                     network=bool(NETWORK_COMMANDS & set(runs)))


ROLES: dict[str, Role] = {r.name: r for r in [
    Role(
        "triage", "Triage lead",
        "Reads a request and decides which runbook fits it and which traits it has.",
        "Do not create, edit or run anything. Choose exactly one runbook from the catalog. Set a "
        "trait to true only when the request needs it. Prefer the runbook whose `use_when` matches "
        "best; a question to answer needs no code.",
    ),
    Role(
        "analyst", "Requirements analyst",
        "Turns a brief into an agreed spec or plan, asking the user only what they alone can decide.",
        "Do not create, edit or run anything. Read files in the working directory only to build on "
        "work that already exists there. Put a decision in `questions` only when it blocks the work "
        "and only the user can make it; otherwise decide it yourself and record it in `assumptions`. "
        "Make every item specific enough that someone else can check it.",
        asks=True,
    ),
    Role(
        "architect", "API designer",
        "Designs the contract between a frontend and a backend before either is built.",
        "Do not create, edit or run anything. Design the smallest API that serves every acceptance "
        "criterion: resource-oriented paths under /api/, JSON bodies, conventional status codes "
        "(201 on create, 404 for a missing resource, 422 for invalid input). Give a literal example "
        "for every request and response body.",
    ),
    Role(
        "tester", "Tester",
        "Writes tests from a spec before the code exists, so they fail for the right reason; and "
        "reviews any change a developer proposes to them.",
        "Write only the test files the task names. Test what a user or a caller observes, through "
        "public interfaces, with literal expected values. Name every test after its criterion id. "
        "Never skip, focus or mark a test todo. Run the test command once to confirm the tests load "
        "and fail because the feature is missing, then stop.",
        edit=True, runs=("test",),
    ),
    Role(
        "developer", "Developer",
        "Writes the code that makes a project's tests pass and its build serve.",
        "Edit files only where the task allows. Write code a senior engineer would approve: small "
        "single-purpose modules, clear names, no dead or commented-out code, no duplication, and no "
        "features the spec does not ask for. Run the build and the tests to check your work; when "
        "they pass, reread your code once, simplify it and run them again.",
        edit=True, runs=("test", "build"),
    ),
    Role(
        "verifier", "Acceptance verifier",
        "Runs a test suite and judges whether each acceptance criterion is really covered by a passing test.",
        "Do not modify files. Run the test command exactly as given, then read the tests and decide for "
        "each criterion whether a passing test really checks it. Copy the counts from the run; never "
        "estimate them.",
        runs=("test",),
    ),
    Role(
        "researcher", "Researcher",
        "Answers one research question from public sources, quoting each source exactly.",
        "Answer only the sub-question you are given, from pages you actually read. There is no "
        f"built-in web search. Search with `{SEARCH} \"<query>\"` (add `--sources` to choose from "
        "wikipedia, openalex, hackernews, github, stackoverflow, npm), then read the best pages "
        "with WebFetch or `curl -sL <url>` (read the output; never save pages to files, which "
        "are deleted). Every finding quotes one or two sentences copied character for "
        "character from the page at its source_url, so a script can find the quote on that page. "
        "Prefer primary and reputable sources. Never state anything you did not read; record what "
        "you could not find in `gaps`.",
        web=True, runs=("search", "curl"),
    ),
    Role(
        "auditor", "Auditor",
        "Reviews existing code through one lens (security, correctness, maintainability, ...) and "
        "reports only problems it can point to.",
        "Do not modify or run anything. Read the code. Report only real problems you can point to: "
        "for each, the file path relative to the working directory, the line, and evidence copied "
        "exactly from that line or the lines next to it. Prefer a few important findings to many "
        "trivial ones. A clean result is a valid result.",
    ),
    Role(
        "writer", "Writer",
        "Turns a plan and verified evidence into a clear, well-cited report.",
        "Do not create, edit or run anything. Use only the evidence you are given, and never state "
        "it more strongly than it allows. Lead with the answer, then the evidence, then the gaps "
        "and assumptions. Write for the audience the plan names, in plain, direct prose.",
    ),
    Role(
        "reviewer", "Reviewer",
        "Reviews a deliverable against its brief, as a demanding editor and fact-checker.",
        "Do not create, edit or run anything. Approve only if the deliverable does what the brief "
        "and plan ask, every claim is backed by the evidence given, and nothing is stated more "
        "strongly than that evidence allows. Otherwise list each specific change needed.",
    ),
]}
