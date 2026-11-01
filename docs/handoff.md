# Handoff: `plaktoz/feat-thinkbig`

Date: 2026-10-31

## 1. State

| Item | Value |
|---|---|
| Branch | `plaktoz/feat-thinkbig`, pushed to `origin`. Head is `ddbcfb9`. |
| Worktree | `/Users/tianhonglee/orca/workspaces/a2a-poc/feat-thinkbig` |
| Pull request | None opened. The branch is many commits ahead of `main`. |
| Tests | `uv run pytest` prints `141 passed`. |
| Lint | `uvx ruff check --select F src tests` reports 1 error: an unused `Path` import in `tests/test_runbooks.py`. It is old and not from this work. |
| Working tree | Clean, except this file. |

## 2. What the project is

A runbook-style A2A software factory in `src/factory`. Runbooks (YAML in `runbooks/`) define the steps. Roles are in `roles.yaml`, staffing in `agents.yaml`, and workers, models and budgets in `llm.yaml`. `README.md` explains the design. `docs/software-factory-prd.md` is the requirements document (now v0.4). `docs/code-review.md` lists the findings and the actions. `docs/tasks.md` is the task list with the status of each task. Read it first to continue the work.

## 3. Done in this session

| Commit | What |
|---|---|
| `4b05723` | Merged `MODELS`, `SHAPES` and `_CHECKS` into one `SCHEMAS` dict (first step of action F). |
| `adb492e` | PRD v0.3: the AXI requirements FR-20 to FR-29. Per-item advice for action J in the review. |
| `9ebee1d` | Project rules in `CLAUDE.md`. |
| `b30d7f0` | Action D: every LLM call logs its model, tokens and cost. See section 4. |
| `ec51ecc` | Reworded `CLAUDE.md`. Rule 1 now has no budget action. |
| `ddbcfb9` | PRD v0.4: open question 8 resolved. See section 5. |

## 4. How usage logging works

- `invoke()` returns the reply and a usage record: worker, model, input tokens, cached input tokens, output tokens and cost.
- Claude reports a cost. Codex reports tokens only, so its cost is `null`. The totals count those calls in `calls_without_cost`.
- The agent puts the usage of every call in its reply. A failed task carries the usage it spent in its error message (`TaskError`).
- `run.json` holds a `usage` object (totals and one entry for each model) and a `calls` list (one row for each call). The console prints a `Usage:` line at the end.
- Usage is logged only. No run or day limit exists. `per_task_usd` is optional and not required: when set, it caps one Claude call (`--max-budget-usd`). When unset, there is no cap. `per_day_usd` is parsed and never read.
- Tests are in `tests/test_usage.py` (17 tests).

## 5. Decisions the owner made

| Topic | Decision |
|---|---|
| Action D | Log only. No budget for now. It must be easy to track. |
| Action F | Do the recommendation: the `SCHEMAS` merge only. The rest is deferred. |
| PRD question 8 | A run waits at a checkpoint with `awaiting: approval` and the command that answers it. `--yes` is the explicit approval for an agent. The run must record who approved. |
| P10 (work on a real repo) | Build it, with its own plan first. The scratch folder is outside the repo, for example `~/factory-scratch/<repo>/<feature>`. One branch for each feature, named from the brief. The lock is per repo and branch. The factory commits the change and tells the person to review it. Several features of one repo can run together. |

## 6. Next steps

1. **P10 plan.** Write a plan before any code. Slice 1: a worktree per feature with a commit as the output. Slice 2: pull request creation.
2. **Small items for action J.** P7 (write the git commit and the gate names into `run.json`) and P11 (a run ID in each request and log line).
3. **Items that D unlocked.** P5 (switch worker on the last retry), P12 (`factory run --resume`), P9 (a read-only `factory runs` command), P6 (secret scan, lint, package-exists check).
4. **AXI work (FR-20 to FR-29).** Not built. The gaps are in the table in action J of `docs/code-review.md`. The `factory approve` command and the approver record from question 8 belong here.
5. **Leftovers.** S4 (strings for `Template.suite` and `Template.serve`), S5, S8, the shared skeleton of the `vitest` and `pytest` gates (S6), the rest of action F, and the unused `Path` import.
6. **Do not build** P13. Do not add risk fields (P8).

## 7. Rules for the next session

`CLAUDE.md` holds the project rules. In short:

- Before every commit, update `README.md`. It is the only README. The old `docs/README.md` and `docs/README.html` were merged into it.
- After code edits, check the test count.
- Run independent work in parallel with subagents.

## 8. Things to know

- The doc checks I used were scripts in `/tmp` (`verify.py`, `lint.py`, `linecheck.py`, `fixlines.py`). They are not in the repo and may be gone. `fixlines.py` could not read the `CODEX_TIMEOUT, CLAUDE_TIMEOUT = 900, 1500` line, so fix that table row by hand. Prose citations such as "`invoke.py` line 128" are not checked by any script, so check them by hand.
- The docs use controlled English: sentences of 20 words or fewer, and no banned words (see the `ste-docs` skill).
- No person has read the merged `README.md` for clarity yet.
- The live run (an LLM call through the whole factory) was not run. The tests use fake agents.
- Use `uv run python` for scripts that import `factory`. The stash is shared across worktrees, so do not use a bare `git stash`.
