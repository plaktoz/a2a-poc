# Software factory: runbooks of A2A agents

You describe what you want. The factory picks a **runbook** for it, sets the runbook's **traits**, staffs each **role** with an LLM worker, and runs the steps. Each role is an A2A agent, and the orchestrator checks every result with **gates** before the next step runs.

The factory isn't limited to building apps. A runbook can build software (frontend, backend or both), research a question, audit existing code or design an API.

```bash
uv sync
uv run factory runbooks                                   # what the factory can do
uv run factory plan web-app --trait backend=yes           # what a run would do, without running it
uv run factory run --brief "a shared shopping list for my family"
```

`run` asks a triage agent to choose the runbook and traits, then shows the choice and the plan and waits for your OK. Skip triage with `--runbook`, and override its choices with `--trait` and `--staff`:

```bash
uv run factory run --brief "a kanban board with tickets and columns"            # frontend only
uv run factory run --brief "a URL shortener API" --runbook web-app \
    --trait frontend=no --trait backend=yes                                      # backend only
uv run factory run --brief "a family shopping list" --trait backend=yes          # full stack
uv run factory run --brief "which vector database fits a 10M-document RAG system?"  # research
uv run factory run --runbook code-audit --target apps/kanban-react --brief "is it ready to ship?"
uv run factory run --brief "..." --staff writer=codex-gpt --staff verifier=codex-gemini
uv run factory run --brief "..." --yes      # unattended: no questions, checkpoints auto-accepted
```

Requirements: the environment variables named in `llm.yaml` (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `GOOGLE_GEMINI_BASE_URL`, `GEMINI_API_KEY`), plus `codex` and `claude` on your `PATH`. Building a frontend also needs `npm`. Claude Code prints a warning that it does not recognize `claude-advantage-assist`; it is expected and the call still works.

`codex-gemini` is enabled in `llm.yaml`, and the verifier role uses it by default, so `GEMINI_API_KEY` and `GOOGLE_GEMINI_BASE_URL` must be set. Codex warns that it has no metadata for `gemini-3.6-flash` and uses fallback metadata.

## Runbooks

| Runbook | Traits | Steps | Output |
|---|---|---|---|
| `web-app` | `frontend` (default yes), `backend` (default no) | spec → contract (full stack only) → tests per surface → loop {develop, verify} | `SPEC.json`, `API.json`, `web/`, `api/` |
| `research` | `review` (default yes) | plan → investigate each sub-question in parallel → loop {report, review} | `PLAN.json`, `REPORT.md` |
| `code-audit` | (needs `--target`) | audit per lens (security, correctness, maintainability, tests) in parallel → loop {report, review} | `AUDIT.md` |
| `api-design` | | spec → loop {contract, review} | `SPEC.json`, `API.json` |

A runbook is a YAML file in `runbooks/`, so adding a kind of work means writing data, not changing the engine. A step names:

- `role`: who does it (see below)
- `instructions`: the task, with `{{ brief }}`, `{{ item.* }}`, `{{ surfaces }}`, `{{ test_files }}` and similar variables
- `output`: the schema its result must match (`spec`, `api_contract`, `research_plan`, `findings`, `audit_findings`, `report`, `verdict`, ...), checked and sent back to the agent when wrong
- `gates`: the checks the orchestrator runs itself
- `when`: a trait condition such as `frontend and backend`
- `for_each`: `surfaces`, a list, or `<step>.<field>`, with `parallel: true` to fan out
- `reads`: earlier steps whose results are the agent's context
- `writes`: `none`, `tests` or `source`. Files changed outside a step's role are reverted.
- `checkpoint`: you review the result before it's used. `save` writes it to a file.

A `loop` groups steps that repeat until they pass, up to the round budget. A failing gate starts a new round with its message as feedback, and in a for-each loop only the failed items run again. `scaffolds` places a template per surface directory when its trait condition holds. Runbooks are validated when they load: unknown roles, outputs, gates, traits, variables or step references are errors with a reason.

Try a combination before running it with `uv run factory plan <runbook> --trait ...`, or add `--mermaid` for a flowchart.

## Roles and staffing

| Role | Does | Tools |
|---|---|---|
| triage | picks the runbook, traits and name for a brief | |
| analyst | turns a brief into a spec or a research plan; may ask you questions | |
| architect | designs the API contract | |
| tester | writes acceptance tests before the code; reviews any test change the developer makes | edit, test |
| developer | builds to the tests | edit, test, build |
| verifier | runs the tests and judges each criterion | test |
| researcher | answers one sub-question with sourced, quoted findings | web search, fetch |
| auditor | reviews code through one lens, citing file:line evidence | read |
| writer | writes the report from verified material only | |
| reviewer | approves or sends back a deliverable | |

The roles themselves are data in `roles.yaml`: each has a `title`, a `description`, a standing `prompt`, and capabilities (`edit`, `web`, `runs`, `asks`). Edit a prompt or a capability there, or add a block for a new role, and no code changes. The file is validated when it loads. Set `FACTORY_ROLES=/path/to/roles.yaml` to use another file; the agents the factory launches inherit it.

`agents.yaml` says which worker (and optionally which model) from `llm.yaml` plays each role. A runbook can override this with its own `staff:`, and `--staff ROLE=WORKER[:MODEL]` overrides it for one run. A checker can't share its author's worker and model, so the reviewer differs from the writer and from the architect, and the verifier from the developer, and a plan that breaks this is refused.

Each run starts one A2A agent per role it needs (`python -m factory.agent ROLE --worker W --port N`) on free ports and checks their cards before sending work. Roles map to tool permissions: Claude `--allowedTools`, or the Codex sandbox (`read-only` / `workspace-write`, with network access for researchers).

## Gates: what the orchestrator checks itself

| Gate | Checks |
|---|---|
| `tests_named` | every acceptance criterion on the surface has a test named after it (`AC3: ...`, `test_ac3_...`) |
| `tests_red` / `tests_green` | the orchestrator's own run of the suite (Vitest or pytest) loads and fails before the build, and passes after it |
| `serves` | the build serves: `vite build` + preview, or uvicorn answering `/openapi.json` with the contract's paths |
| `counts_match`, `criteria_covered` | the verifier's pass/fail counts match the orchestrator's run, and every criterion is PASS |
| `citations_verified` | each research finding's quote is on the page it cites (fetched by the orchestrator). Unverifiable findings are dropped. |
| `sources_cited` | the report links verified sources, and only those |
| `evidence_verified` | each audit finding's evidence is in the cited file near the cited line, inside the target |
| `review_approves` | the reviewer approved. Otherwise its issues go back to the author. |

A developer may change a test only with a stated reason, and the tester approves the change or it's reverted.

## Templates

A template is a scaffold directory in `templates/<name>/` with metadata in `templates/<name>.yaml`: the stack notes the agents get, the test command and suite, how to serve it, which files are tests, which are source, and which are protected. Shipped templates are `react-vite` (React, Vitest, Testing Library) and `fastapi` (FastAPI, pytest, uv). In a full-stack app the web dev server proxies `/api` to the backend.

## Web search

Researchers can't rely on native web search through the gateway. They use the factory's own search, which queries public APIs:

```bash
uv run python -m factory.search "sqlite concurrent writes" --sources wikipedia,openalex,hackernews,github,stackoverflow,npm --limit 5
```

They then fetch the pages they cite, and the `citations_verified` gate checks every quote against the page.

## Runs

Work happens in `apps/<name>/`, one directory per run, so runs can overlap. `apps/<name>/.factory/` holds `run.json` (status, steps, attempts, gate results), `artifacts.json` and each agent's log. A second run of the same name is refused while the first holds the lock, and accepted work isn't redone without `--rebuild`.

## Code

`src/factory/`: `cli.py` (commands), `runbook.py` (loading, validation, trait resolution, staffing, plan outline), `engine.py` (runs a plan: steps, loops, retries, role checks, checkpoints), `gates.py`, `schemas.py` (deliverables), `roles.py` (loads `roles.yaml`), `agent.py` (the generic role agent), `fleet.py` (launches agents), `invoke.py` (Claude / Codex calls with per-role tools), `scaffold.py`, `search.py`, `a2a_transport.py`.

Tests: `uv run pytest`. The engine tests use fake agents, so no LLM is called.
