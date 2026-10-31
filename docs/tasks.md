# Task list: `plaktoz/feat-thinkbig`

Date: 2026-10-31

This file is the single list of work on this branch. An agent that starts or restarts work reads it first.
Update a task's status in this file whenever the task changes state, in the same commit as the change.

## 1. How to continue after a stop

1. Run `git status --short` and `git log --oneline -15`.
2. Read the status table in section 3. Find the first task that is not `committed`.
3. If the task is `in progress`, compare its files with `git status`. Finish the open checklist items.
4. If the task is `built`, run its verification in section 4. Fix what fails.
5. Update `README.md` and the test count. Commit the task alone. Set its status to `committed` and record the commit.
6. Start the next task whose dependencies are `committed`.

Status values:

| Status | Meaning |
|---|---|
| `todo` | Not started. |
| `in progress` | Partly built. The checklist shows what is left. |
| `built` | Code and tests are in the working tree. The full suite passes. Not committed. |
| `committed` | Verified and committed. The commit is recorded. |
| `not built` | The owner decided not to build it. |

## 2. Rules for each task

- Verify the task yourself before the commit. Run the full suite with `.venv/bin/pytest -q`. Run the command the task changed.
- Ask a person only when there is no other option.
- Commit one task per commit. Do not push.
- Do not commit a file that a running subagent is editing.
- Several tasks share `engine.py`, `cli.py` and `runbook.py`. Use `git add -p` to split their hunks.

## 3. Status

Suite at the last check: 225 passed.

| Task | What | Depends on | Status | Commit |
|---|---|---|---|---|
| P19 | Worker plugins: `plugin:` module path in `llm.yaml` | none | built | |
| P6 | Gates `secrets_clean`, `lint_clean`, `packages_exist` | none | built | |
| P7 + P11 | Run ID, git commit and gate names in `run.json` | none | built | |
| P17 | Triage can answer `none` | none | built | |
| P12 | `factory run --resume` | P7 | built | |
| P9 | `factory runs [--json]` | P7 | built | |
| P5 | Per-role `fallback` on the last retry | P19 | built | |
| Budget | `budgets.per_task_usd` optional, not required | none | built | |
| P10 slice 1 | Worktree, `change-request` runbook, commit | P19 | in progress | |
| P20 | Run task list, so a stopped run continues from the last done sub-task | P12 | todo | |
| P10 slice 2 | `--pr`: push and `gh pr create`, with approval | P10 slice 1, P6 | todo | |
| AXI | FR-20 to FR-29: `factory approve`, approver record, TOON output, `help[]` lines | P12 | todo | |
| Leftovers | C3, S4, S5, S6, S8, rest of F, unused `Path` import | none | todo | |
| P13 | A2A direct-call adapter | | not built | |
| P8 | Risk fields | | not built | |
| P18 | Windows | | not built (deferred) | |

## 4. Tasks

### P19: worker plugins

- Files: `src/factory/workers/` (`base.py`, `loader.py`, `claude.py`, `codex.py`), `invoke.py`, `config.py`, `llm.yaml`, `tests/test_workers.py`, `tests/fake_worker*.py`.
- Verify: full suite. `harness:` in `llm.yaml` fails with a message that names `plugin:`.
- [x] Interface `name`, `validate`, `run`.
- [x] Move `claude` and `codex` into plugins.
- [ ] README: the plugin interface section.
- [ ] Commit.

### P6: three new gates

- Files: `src/factory/gates.py`, `runbooks/web-app.yaml`, `tests/test_gates_p6.py`.
- Verify: full suite. `factory plan web-app` lists the new gates.
- [x] `secrets_clean`, `lint_clean`, `packages_exist`.
- [ ] README: the gate list. PRD section 8.4 notes.
- [ ] Commit.

### P7 + P11: run ID and run record

- Files: `src/factory/engine.py`, `agent.py`, `cli.py`, `tests/test_tracing.py`.
- Verify: full suite. A fake run prints `Run <id>`. `run.json` has `run_id`, `factory` and `gates`.
- [ ] README: the run record fields.
- [ ] Commit.

### P17: no runbook fits

- Files: `src/factory/schemas.py`, `roles.yaml`, `cli.py`, `tests/test_schemas.py`.
- Verify: full suite. The CLI test for `none` exits with code 1 and makes no workspace.
- [ ] README: the `none` answer.
- [ ] Commit.

### P12: resume

- Files: `src/factory/engine.py`, `cli.py`, `tests/test_resume.py`.
- Verify: full suite. `--resume` without `--name` fails. An accepted run is refused.
- [ ] README: `--resume` in the command table.
- [ ] Commit.

### P9: `factory runs`

- Files: `src/factory/runs.py`, `cli.py`, `tests/test_runs.py`.
- Verify: full suite. Run `factory runs` and `factory runs --json` on `apps/`.
- [ ] README: the command table.
- [ ] Commit.

### P5: fallback worker

- Files: `src/factory/runbook.py`, `fleet.py`, `engine.py`, `cli.py`, `tests/test_fallback.py`.
- Verify: full suite. A fallback with the same worker and model fails to load. A fallback that breaks independence fails to load.
- [ ] README: the `fallback` field in the `agents.yaml` section.
- [ ] Commit.

### Budget: `per_task_usd` optional

- Files: `src/factory/config.py`, `workers/claude.py`, `llm.yaml`, `tests/test_config.py`, `tests/test_usage.py`, docs.
- Verify: the shipped `llm.yaml` loads with `per_task_usd = None`. No `--max-budget-usd` flag without a cap.
- [x] Code and tests.
- [x] README, `docs/handoff.md`, `docs/code-review.md`, PRD.
- [ ] Commit.

### P10 slice 1: worktree and commit

- Plan: `docs/p10-plan.md`, section 9 holds the checklist.
- Files: `src/factory/repo.py`, `runbooks/change-request.yaml`, `engine.py`, `gates.py`, `cli.py`, `tests/test_repo.py`.
- Verify: full suite. A run with fake agents on a git repo in a temporary folder ends with one commit on `factory/<feature>`. The original checkout is unchanged.

### P20: run task list

Goal: a run that stops (tokens used up, LLM down, network down) continues from its last done sub-task. Today `--resume` keeps accepted items only. A half-done item starts again.

- [ ] The engine writes `.factory/tasks.json` and `.factory/TASKS.md` when the run starts. One entry per step and item: `todo`, `running`, `accepted` or `failed`.
- [ ] `factory plan` and the run start print the task list.
- [ ] Each agent request carries the item's sub-task list. The agent marks each sub-task done in its reply and in the file.
- [ ] `--resume` sends the open sub-tasks only, and the work already in the folder.
- [ ] A stop from an LLM or network error prints the `--resume` command.
- [ ] Tests with fake agents: stop mid-item, resume, check that done sub-tasks are not sent again.
- [ ] README and test count. Commit.

### P10 slice 2: pull request

- Depends on P10 slice 1 and P6 (secret scan).
- Tests use fakes only. Do not run a real `git push` or `gh pr create` without the owner's go-ahead.

### AXI: FR-20 to FR-29

- See `docs/code-review.md` section 4.

### Leftovers

- See `docs/code-review.md` section 1.
