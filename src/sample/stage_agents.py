"""The four pipeline stages as A2A agents, one server per stage.

Each agent wraps the LLM worker and model that pipeline.yaml names for its stage, and
serves A2A at that stage's url. pipeline.py starts them itself; to run one by hand:

  uv run python src/sample/stage_agents.py requirements [--stages PATH] [--port N]

--port overrides the port in the stage's url. pipeline.py uses it to give every run its own
agents, so several apps can be built at once.

Requests and results are JSON (A2A data parts):

  requirements  {brief, workdir}
                -> input-required {kind: "questions", questions, spec}  answer {answers: [str]}
                -> input-required {kind: "review", spec}                answer {accept: true}
                                                                     or {accept: false, changes}
                -> {spec}
  tests         {spec, workdir, feedback}  -> writes src/App.test.jsx, {path, tests}
  implement     {workdir, feedback}        -> edits src/, {summary}
  verify        {workdir}                  -> {passed, failed, verdicts: {AC1: "PASS" | "FAIL - ..."}}

POC limitation: the agents work in the orchestrator's workdir on the same machine. A
remote agent would need the workspace shared through git instead.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn

from factory import ConfigError, claude_env, codex_argv, load_config
from factory.a2a_transport import Reply, Turn, agent_card, build_app
from pipeline_common import (
    DEFAULT_STAGES, ROOT, STAGE_NAMES, TEST_FILE, Stage,
    check_spec, code_block, load_stages, run, spec_text, tail,
)  # fmt: skip

CODEX_TIMEOUT, CLAUDE_TIMEOUT = 600, 1200
LLM_ATTEMPTS = 2  # per request, when the reply is malformed

ROLES = {
    "requirements": ("Requirements analyst",
                     "Turns a brief into a spec with acceptance criteria, asks the user what only "
                     "they can decide, and returns the spec once the user accepts it."),
    "tests": ("Tester", "Writes Vitest + React Testing Library acceptance tests, at least one "
                        "per acceptance criterion, before the app exists."),
    "implement": ("Developer", "Builds the React app until the acceptance tests pass."),
    "verify": ("Acceptance verifier", "Runs the tests and checks that each acceptance criterion "
                                      "is really covered by a passing test."),
}  # fmt: skip

STACK = """\
Project stack (fixed; do not change it):
- React 19 + Vite, plain JavaScript with JSX (no TypeScript).
- src/main.jsx renders the default export of src/App.jsx and imports src/App.css.
- Tests: Vitest + jsdom + @testing-library/react + @testing-library/user-event, with jest-dom
  matchers. src/setupTests.js clears localStorage before each test and cleans up after it.
- No new dependencies: package.json is fixed.
"""

SPEC_FORMAT = """\
Reply with exactly one ```json code block and nothing after it, with this shape:
{"title": str, "goal": str,
 "acceptance_criteria": [{"id": "AC1", "text": str}],
 "constraints": [str], "out_of_scope": [str], "assumptions": [str], "questions": [str]}
Rules:
- Each acceptance criterion is one behaviour a user can observe in the browser, testable with
  React Testing Library in jsdom. Drag and drop must not be the only way to do something.
- Ids are AC1, AC2, ... in order. Keep v1 small: 4 to 8 criteria.
- "questions" lists decisions only the user can make; leave it empty when nothing blocks the spec.
"""


def llm(cfg, stage: Stage, prompt: str, workdir: Path, *, edits: bool,
        commands: tuple[str, ...] = ()) -> str:  # fmt: skip
    """Run this stage's worker on `prompt` in `workdir` and return its final reply.

    `edits` lets it write files. `commands` are the shell commands it may run, as Claude Code
    tool patterns such as "npx vitest run:*". Codex has no per-command allowlist, so any
    command makes its sandbox writable; the orchestrator checks which files changed.
    """
    harness = cfg.worker(stage.worker).harness
    if harness == "codex":
        sandbox = "workspace-write" if edits or commands else "read-only"
        argv = codex_argv(cfg, stage.worker, prompt, model=stage.model, sandbox=sandbox, cd=workdir)
        argv.insert(argv.index("--"), "--skip-git-repo-check")
        r = run(argv, cwd=workdir, timeout=CODEX_TIMEOUT)
    elif harness == "claude-agent-sdk":
        # Clean env (a session's CLAUDE_CODE_* variables hang the nested call). Tools are an
        # allowlist, so Bash only runs the listed commands.
        env = {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"],
               **claude_env(cfg, stage.worker, model=stage.model)}  # fmt: skip
        tools = ["Read", "Glob", "Grep", *(["Edit", "Write"] if edits else []),
                 *(f"Bash({c})" for c in commands)]  # fmt: skip
        argv = [
            "claude", "-p", prompt, "--model", stage.model,
            "--permission-mode", "acceptEdits",
            "--max-budget-usd", str(cfg.budgets.per_task_usd),
            "--allowedTools", *tools,
        ]  # fmt: skip
        r = run(argv, cwd=workdir, env=env, timeout=CLAUDE_TIMEOUT)
    else:
        raise ConfigError(f"stage {stage.name!r}: harness {harness!r} is not supported")
    if r.returncode != 0:
        raise RuntimeError(f"{stage.worker} exited {r.returncode}: {tail(r, 500)}")
    return r.stdout


# --- requirements -----------------------------------------------------------


def draft_spec(cfg, stage: Stage, workdir: Path, history: str) -> dict:
    problem = ""
    for _ in range(LLM_ATTEMPTS):
        out = llm(
            cfg, stage,
            "You are the requirements analyst for a small web app. Do not edit, create or run "
            f"anything. Turn the user's brief into a spec.\n\n{STACK}\n{SPEC_FORMAT}\n{history}",
            workdir, edits=False,
        )  # fmt: skip
        block = code_block(out, "json")
        try:
            if block is None:
                raise ValueError("the reply had no ```json block")
            return check_spec(json.loads(block))
        except ValueError as e:  # json.JSONDecodeError is a ValueError
            problem = str(e)
            history += f"\nYour previous reply was rejected: {e}. Follow the reply format exactly.\n"
    raise RuntimeError(f"no valid spec after {LLM_ATTEMPTS} attempts: {problem}")


def requirements(cfg, stage: Stage, turns: list[Turn]) -> Reply:
    first, last = turns[0]["data"], turns[-1]["data"]
    if len(turns) > 1 and turns[-2]["data"]["kind"] == "review" and last.get("accept"):
        return Reply({"spec": turns[-2]["data"]["spec"]})

    # Rebuild the conversation: each of our questions or drafts, then the user's answer.
    history = f"The user's brief:\n{first['brief']}\n"
    for ask, answer in zip(turns[1::2], turns[2::2]):
        ask, answer = ask["data"], answer["data"]
        history += f"\nYour previous draft:\n```json\n{json.dumps(ask['spec'], indent=2)}\n```\n"
        if ask["kind"] == "questions":
            qa = [f"Q: {q}\nA: {a}" for q, a in zip(ask["questions"], answer.get("answers", []))]
            history += "\nThe user's answers:\n" + "\n".join(qa) + "\n"
        else:
            history += f"\nThe user asked for these changes:\n{answer.get('changes', '')}\n"

    spec = draft_spec(cfg, stage, Path(first["workdir"]), history)
    if spec["questions"]:
        return Reply({"kind": "questions", "questions": spec["questions"], "spec": spec}, final=False)
    return Reply({"kind": "review", "spec": spec}, final=False)


# --- tests ------------------------------------------------------------------


def tests(cfg, stage: Stage, turns: list[Turn]) -> Reply:
    req = turns[-1]["data"]
    spec, workdir, feedback = check_spec(req["spec"]), Path(req["workdir"]), req.get("feedback", "")
    ids = [c["id"] for c in spec["acceptance_criteria"]]
    problem = ""
    for _ in range(LLM_ATTEMPTS):
        out = llm(
            cfg, stage,
            "You are the tester. Write the acceptance tests for a React app before it is built.\n"
            "Do not edit, create or run anything. Reply with exactly one ```jsx code block holding "
            f"the full contents of {TEST_FILE}, and nothing else.\n"
            "Rules:\n"
            "- Import only from 'vitest', '@testing-library/react', '@testing-library/user-event' "
            "and './App' (default export App).\n"
            "- Test what a user sees: render <App />, interact through userEvent, query by role, "
            "label or visible text. No class names, test ids, internal state or private details.\n"
            "- Every acceptance criterion has at least one test, and every test name starts with "
            "its criterion id and a colon, for example it('AC2: ...').\n"
            "- Use literal expected values. No .skip, .only or .todo.\n"
            "- The UI does not exist yet, so choose plain, conventional accessible names. The "
            "developer builds to the names your tests use.\n\n"
            f"{STACK}\nAccepted spec:\n{spec_text(spec)}\n"
            + (f"\n{feedback}\n" if feedback else "")
            + (f"\nYour previous reply was rejected: {problem}\n" if problem else ""),
            workdir, edits=False,
        )  # fmt: skip
        block = code_block(out, "jsx|javascript|js")
        if block is None:
            problem = "the reply had no ```jsx code block"
            continue
        named = set(re.findall(r"\b(?:it|test)\(\s*['\"`](AC\d+):", block))
        if missing := [i for i in ids if i not in named]:
            problem = f"no test named for {', '.join(missing)}"
            continue
        if re.search(r"\.(?:skip|only|todo)\(", block):
            problem = "the tests use .skip, .only or .todo"
            continue
        # The tester's LLM runs read-only; the agent writes its deliverable.
        (workdir / TEST_FILE).write_text(block.strip() + "\n")
        return Reply({"path": TEST_FILE, "tests": len(re.findall(r"\b(?:it|test)\(", block))})
    raise RuntimeError(f"no usable test file after {LLM_ATTEMPTS} attempts: {problem}")


# --- implement --------------------------------------------------------------


def implement(cfg, stage: Stage, turns: list[Turn]) -> Reply:
    req = turns[-1]["data"]
    feedback = req.get("feedback", "")
    out = llm(
        cfg, stage,
        f"You are the developer. Build the app so that every test in {TEST_FILE} passes.\n"
        f"Read SPEC.json and {TEST_FILE} first. Create or edit files only under src/. Do not "
        f"edit {TEST_FILE}, src/main.jsx or src/setupTests.js, and do not add dependencies.\n"
        "Put the app in src/App.jsx (default export App) and its styles in src/App.css; you may "
        "add components under src/. Make it clean and usable, but build only what the spec and "
        "tests ask for.\n"
        "You may run `npm run build` and `npx vitest run` to check your work.\n\n"
        f"{STACK}" + (f"\n{feedback}\n" if feedback else ""),
        Path(req["workdir"]), edits=True, commands=("npm run build:*", "npx vitest run:*"),
    )  # fmt: skip
    return Reply({"summary": out.strip()[-1500:]})


# --- verify -----------------------------------------------------------------


def verify(cfg, stage: Stage, turns: list[Turn]) -> Reply:
    workdir = Path(turns[-1]["data"]["workdir"])
    spec = check_spec(json.loads((workdir / "SPEC.json").read_text()))
    ids = [c["id"] for c in spec["acceptance_criteria"]]
    for _ in range(LLM_ATTEMPTS):
        out = llm(
            cfg, stage,
            "You are the acceptance verifier. Do not modify any files.\n"
            "1. Run this exact command in the current directory: npx vitest run\n"
            f"2. Read SPEC.json and {TEST_FILE}. For each acceptance criterion, decide whether a "
            "passing test really checks it.\n"
            "Reply with one line per criterion, `AC<n>: PASS` or `AC<n>: FAIL - <reason>`, and as "
            "the very last line exactly `RESULT: passed=<N> failed=<M>` with the counts from the "
            "test run (skipped or errored tests count as failed).",
            workdir, edits=False, commands=("npx vitest run:*",),
        )  # fmt: skip
        if m := re.findall(r"RESULT:\s*passed=(\d+)\s+failed=(\d+)", out):
            verdicts = {i: v.strip() for i, v in re.findall(r"^\W*(AC\d+)\W*:\s*(.+)$", out, re.M) if i in ids}
            return Reply({"passed": int(m[-1][0]), "failed": int(m[-1][1]), "verdicts": verdicts})
    raise RuntimeError(f"no RESULT line after {LLM_ATTEMPTS} attempts:\n{out[-600:]}")


HANDLERS = {"requirements": requirements, "tests": tests, "implement": implement, "verify": verify}


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve one pipeline stage as an A2A agent.")
    ap.add_argument("stage", choices=STAGE_NAMES)
    ap.add_argument("--stages", type=Path, default=DEFAULT_STAGES, help="pipeline.yaml")
    ap.add_argument("--port", type=int, help="serve on this port instead of the url's")
    args = ap.parse_args()

    cfg = load_config(ROOT / "llm.yaml")
    stage = load_stages(cfg, args.stages.resolve())[args.stage]
    if args.port:
        stage = replace(stage, url=f"http://{urlsplit(stage.url).hostname}:{args.port}")
    title, description = ROLES[stage.name]
    card = agent_card(
        name=f"{stage.name}-agent", description=description, url=stage.url,
        skill_id=stage.name, skill_name=title,
        tags=[f"stage:{stage.name}", f"worker:{stage.worker}", f"model:{stage.model}"],
    )  # fmt: skip
    handler = functools.partial(HANDLERS[stage.name], cfg, stage)
    u = urlsplit(stage.url)
    print(f"{card.name} ({stage.worker} / {stage.model}) serving A2A at {stage.url}", flush=True)
    uvicorn.run(build_app(handler, card), host=u.hostname, port=u.port, log_level="warning")


if __name__ == "__main__":
    main()
