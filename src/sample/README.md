# Live gateway samples

Each script sends `Reply with exactly: OK` to one worker, using the endpoint, key and model from `llm.yaml`. A pass prints `exit=0` and `OK`.

## Prerequisites

- `uv sync`
- The environment variables `llm.yaml` names are set: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`
- `codex` and `claude` are on your `PATH`

Run from the repo root.

## Run

```bash
# Codex worker, GPT
uv run python src/sample/codex_live.py

# Claude worker
uv run python src/sample/claude_live.py
```

The Claude run prints a warning that Claude Code does not recognize `claude-advantage-assist`. It is expected, and the call still works.

## Gemini

1. Set `enabled: true` on `codex-gemini` in `llm.yaml`.
2. Set `GEMINI_API_KEY` and `GOOGLE_GEMINI_BASE_URL`.
3. Run `uv run python src/sample/codex_live.py codex-gemini`.

Codex warns that it has no metadata for `gemini-3.6-flash` and uses fallback metadata.

## A2A app pipeline

`pipeline.py` builds an app from your requirements through four A2A agents, one per stage. Each app is built in its own directory, `apps/<name>/`, copied from the scaffold in `templates/react-vite/`.

| Stage | A2A agent does | Orchestrator gate |
|---|---|---|
| 1. requirements | asks you questions, drafts a spec with acceptance criteria | you accept the spec |
| 2. tests | writes `src/App.test.jsx`, one or more tests per criterion | the suite loads and fails (red) |
| 3. implement | builds the React app, and may fix a test that is wrong | only `src/` changed, any test edit approved by the tester (with a reason, reverted if rejected), `npm run build` passes, `vite preview` serves it |
| 4. verify | runs the tests, judges each criterion | its counts match the orchestrator's own Vitest run, all green, every criterion PASS |

A failure in stage 3 or 4 goes back to the developer, up to `1 + budgets.max_retries` rounds.

The orchestrator talks to the agents only over A2A. It reads each agent card, sends a task, and gets the result back as an artifact. Stage 1 runs as one multi-turn task: the agent pauses in `input-required` for your answers and again for your acceptance.

`pipeline.yaml` sets each stage's LLM worker (from `llm.yaml`) and its model (optional, defaults to the worker's default model). Swap a stage's LLM by editing that file, or pass `--stages other.yaml`.

```bash
uv run python src/sample/pipeline.py --app todo --brief "a todo app with due dates"
uv run python src/sample/pipeline.py --app todo --rebuild   # build over an existing app
uv run python src/sample/pipeline.py --app todo --yes       # unattended: the analyst decides open points
uv run python src/sample/pipeline.py --app todo --spec apps/todo/SPEC.json   # skip stage 1
```

`--app` is the app's name (lowercase letters, digits, hyphens). Without it, the default brief builds `kanban-react` and any other `--brief` gets a name made from its first words.

### Building several apps at once

Start one pipeline per app, each in its own terminal:

```bash
uv run python src/sample/pipeline.py --app todo   --brief "a todo app with due dates"
uv run python src/sample/pipeline.py --app notes  --brief "a markdown notes app"
```

Nothing is shared between the runs:

- **Files:** each app has its own `apps/<name>/` with its own `node_modules`, `SPEC.json`, `src/` and `.factory/`.
- **Ports:** each run starts its own four agents on free ports, so runs never talk to each other's agents. The `url` ports in `pipeline.yaml` are only used with `--no-launch`.
- **Logs:** agent logs and the run log are in `apps/<name>/.factory/`.
- **Safety:** an app that already has a `SPEC.json` is not touched unless you pass `--rebuild`, and a second run of the same app is refused while the first is running.

Runs share the LLM gateway, so its rate limits and `budgets.per_task_usd` apply per agent call, not across runs.

To run an agent yourself, for example in another terminal, start it with `uv run python src/sample/stage_agents.py <stage> --port N` and pass `--no-launch` to the pipeline, which then uses the urls in `pipeline.yaml`. The orchestrator checks each agent's card tags against `pipeline.yaml` before it sends work.

The output of a build is `apps/<name>/SPEC.json` and `apps/<name>/src/`, plus a run log in `apps/<name>/.factory/run.json`. Start the app with `cd apps/<name> && npm run dev`. See `apps/kanban-react/README.md` for an example.

POC limit: the agents and the orchestrator share one working directory on one machine. Remote agents would share the workspace through git.

## Notes

- `codex exec` hangs on an open stdin, so `codex_live.py` closes it.
- `claude_live.py` passes a clean environment. Inheriting a session's `CLAUDE_CODE_*` variables made the nested call hang.
- A non-zero exit prints the tail of stderr.
