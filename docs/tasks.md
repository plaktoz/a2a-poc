# Task list: `plaktoz/feat-thinkbig`

Date: 2026-11-01

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

Suite at the last check: 343 passed.

Note: the tasks marked `committed` with "see note" were built together in the working tree and share hunks in `engine.py`, `cli.py`, `gates.py`, `runbook.py`, `config.py`, `llm.yaml` and the tests. They went into one commit ("Build the P-series ..."), because a split could not be verified. From the next task on, commit one task at a time.

| Task | What | Depends on | Status | Commit |
|---|---|---|---|---|
| P19 | Worker plugins: `plugin:` module path in `llm.yaml` | none | committed | the P-series commit, see note |
| P6 | Gates `secrets_clean`, `lint_clean`, `packages_exist` | none | committed | the P-series commit, see note |
| P7 + P11 | Run ID, git commit and gate names in `run.json` | none | committed | the P-series commit, see note |
| P17 | Triage can answer `none` | none | committed | the P-series commit, see note |
| P12 | `factory run --resume` | P7 | committed | the P-series commit, see note |
| P9 | `factory runs [--json]` | P7 | committed | the P-series commit, see note |
| P5 | Per-role `fallback` on the last retry | P19 | committed | the P-series commit, see note |
| Budget | `budgets.per_task_usd` optional, not required | none | committed | the P-series commit, see note |
| Check | `factory check`: one short prompt to each model, before any work | P19 | committed | the P-series commit, see note |
| Codex cost | FR-30: `docs/model-prices.md` gives the cost of Codex calls, refreshed weekly from llm-prices.com | P19 | committed | `68c239d` |
| P10 slice 1 | Worktree, `change-request` runbook, commit | P19 | committed | the P-series commit, see note |
| Bug fix | `bug-fix` runbook: a failing test first, then the fix, in a worktree. `change-request` no longer takes bugs. | P10 slice 1 | committed | `a80a6ba` |
| P20 | Run task list, so a stopped run continues from the last done sub-task | P12 | committed | see `git log` |
| Impact | `impact-analysis` runbook: which services a change affects, across one or more repos. Report only. | P10 slice 1 | committed | `4773e48` |
| P10 slice 2 | `--pr`: push and `gh pr create`, with approval | P10 slice 1, P6 | committed | `37a6a49` |
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
- [x] README: the plugin interface section.
- [ ] Commit.

### P6: three new gates

- Files: `src/factory/gates.py`, `runbooks/web-app.yaml`, `tests/test_gates_p6.py`.
- Verify: full suite. `factory plan web-app` lists the new gates.
- [x] `secrets_clean`, `lint_clean`, `packages_exist`.
- [x] README: the gate list.
- [ ] PRD section 8.4 notes.
- [ ] Commit.

### P7 + P11: run ID and run record

- Files: `src/factory/engine.py`, `agent.py`, `cli.py`, `tests/test_tracing.py`.
- Verify: full suite. A fake run prints `Run <id>`. `run.json` has `run_id`, `factory` and `gates`.
- [x] README: the run record fields.
- [ ] Commit.

### P17: no runbook fits

- Files: `src/factory/schemas.py`, `roles.yaml`, `cli.py`, `tests/test_schemas.py`.
- Verify: full suite. The CLI test for `none` exits with code 1 and makes no workspace.
- [x] README: the `none` answer.
- [ ] Commit.

### P12: resume

- Files: `src/factory/engine.py`, `cli.py`, `tests/test_resume.py`.
- Verify: full suite. `--resume` without `--name` fails. An accepted run is refused.
- [x] README: `--resume` in the command table.
- [ ] Commit.

### P9: `factory runs`

- Files: `src/factory/runs.py`, `cli.py`, `tests/test_runs.py`.
- Verify: full suite. Run `factory runs` and `factory runs --json` on `apps/`.
- [x] README: the command table.
- [ ] Commit.

### P5: fallback worker

- Files: `src/factory/runbook.py`, `fleet.py`, `engine.py`, `cli.py`, `tests/test_fallback.py`.
- Verify: full suite. A fallback with the same worker and model fails to load. A fallback that breaks independence fails to load.
- [x] README: the `fallback` field in the `agents.yaml` section.
- [ ] Commit.

### Budget: `per_task_usd` optional

- Files: `src/factory/config.py`, `workers/claude.py`, `llm.yaml`, `tests/test_config.py`, `tests/test_usage.py`, docs.
- Verify: the shipped `llm.yaml` loads with `per_task_usd = None`. No `--max-budget-usd` flag without a cap.
- [x] Code and tests.
- [x] README, `docs/handoff.md`, `docs/code-review.md`, PRD.
- [ ] Commit.

### Check: `factory check`

- Files: `src/factory/check.py`, `cli.py`, `tests/test_check.py`.
- Verify: full suite. `factory check` against the gateway: all 5 models answered on 2026-10-31 (66,703 tokens, $0.1066).
- [x] Code, tests, README.
- [ ] Commit.

### Codex cost: FR-30

- Files: `src/factory/prices.py`, `docs/model-prices.md`, `src/factory/invoke.py`, `src/factory/check.py`, `tests/test_prices.py`, `tests/test_workers.py`, `.claude/skills/update-model-prices/SKILL.md`.
- Decided by the owner (2026-10-31): the prices come from https://www.llm-prices.com/ (`current-v1.json`). The table lives in `docs/model-prices.md`, not in `llm.yaml`. `gpt-advantage-assist` is priced as `gpt-6.1-sol`. `claude-advantage-assist` is priced as `claude-opus-5-5`.
- Verify: a Codex call with a priced model reports a cost. A model with no price reports `unknown`, and the run total counts it.
- [x] Price table in `docs/model-prices.md`, with the feed date and the fetch date.
- [x] Cost from the table per token type (`prices.cost_usd`), filled in by `invoke` when the worker reports none.
- [x] Tests (`tests/test_prices.py`), README, skill `update-model-prices`.
- [x] PRD v0.11 reference. Commit.

### P10 slice 1: worktree and commit

- Status: built. The suite passes (255). A real-git check made two features in one repo, one commit each, and the original checkout stayed clean.
- Plan: `docs/p10-plan.md`, section 9 holds the checklist.
- Files: `src/factory/repo.py`, `runbooks/change-request.yaml`, `engine.py`, `gates.py`, `cli.py`, `tests/test_repo.py`.
- Verify: full suite. A run with fake agents on a git repo in a temporary folder ends with one commit on `factory/<feature>`. The original checkout is unchanged.

### P20: run task list

Goal: a run that stops (tokens used up, LLM down, network down) continues from its last done sub-task.

- Status: committed. The suite passes (343).
- Decided by the owner (2026-11-01): a sub-task is one acceptance criterion of the spec.
- Files: `src/factory/tasks.py`, `src/factory/engine.py`, `src/factory/schemas.py` (`criteria_done`), `src/factory/cli.py` (`factory plan`), `tests/test_tasks.py`, `README.md`.
- Verify: `.venv/bin/pytest -q`, `.venv/bin/factory plan change-request` (prints the task list).
- A step inside a loop is accepted for good only when the loop is accepted. A failed round reopens its items with a `reopened` event, so a resume runs them again.
- [x] The engine writes `.factory/tasks.json` and `.factory/TASKS.md` when the run starts. One entry per step and item: `todo`, `running`, `accepted` or `failed`.
- [x] `factory plan` and the run start print the task list.
- [x] Each agent request carries the item's sub-task list. The agent marks each sub-task done in its reply (`criteria_done`), and the engine marks it in the file.
- [x] `--resume` sends the open sub-tasks only, and the work already in the folder.
- [x] A stop from an LLM or network error prints the `--resume` command.
- [x] Tests with fake agents: stop mid-item, resume, check that done sub-tasks are not sent again.
- [x] README and test count. Commit.

### Bug fix: `bug-fix` runbook

- Status: committed. The suite passes (282).
- Decided by the owner (2026-10-31): bugs go to `bug-fix`, not `change-request`. `change-request` covers features and deliberate changes only. The fix starts from a test that fails because of the bug.
- Files: `runbooks/bug-fix.yaml`, `runbooks/change-request.yaml` (`use_when` and `description`), `src/factory/gates.py` (`repro_fails`), `src/factory/schemas.py` (`repro`), `src/factory/cli.py` (texts), `tests/test_bugfix.py`, `tests/test_runbooks.py`, `README.md`.
- Verify: `.venv/bin/pytest -q`, `.venv/bin/factory runbooks`, `.venv/bin/factory plan bug-fix`. A fake run makes one commit on `factory/<name>`, and the original checkout stays unchanged.
- Note (fixed 2026-10-31): the developer and the reviewer both used `codex-gpt / gpt-6.1-sol`. The independence rule did not check that pair. Now it does (`_check_independence`), and the reviewer in `agents.yaml` is `codex-gemini / gemini-3.7-flash`. With only one model enabled (for example Claude alone), the plan runs and warns that the best practice is a different model.
- [x] Runbook `bug-fix`: `spec`, `reproduce` (tester, `repro_fails`), then the `build` loop with `fix` and `verify` and `repo_tests_pass`.
- [x] `change-request` `use_when` and `description` cover features and changes only.
- [x] Gate `repro_fails` and schema `repro`.
- [x] CLI texts, `tests/test_runbooks.py`, `tests/test_bugfix.py` (12 tests, fakes and real git repos).
- [x] README: intro, quick start, runbook table, example, "Fix a bug", gate, test table, troubleshooting. Test count 282.
- [x] Commit.

### P10 slice 2: pull request

- Depends on P10 slice 1 and P6 (secret scan).
- Status: committed. The suite passes (330).
- Files: `src/factory/repo.py` (`push_branch`, `create_pr`, `has_remote`, `remote_url`, `current_branch`), `src/factory/engine.py` (`_pull_request`, `_approver`, `Awaiting`, `_pr_body`), `src/factory/gates.py` (`scan_files`), `src/factory/cli.py` (`--pr`), `tests/test_pr.py`, `README.md`.
- Tests use a bare repo in a temporary folder as `origin` and a fake `gh`. No real push and no `gh pr create` ran. A real push to GitHub is not verified. No person has done this check yet.
- Decided: only `--yes` approves the push, because commands never prompt (PRD FR-25). A run without it stops at `awaiting: approval`. The `factory approve` command is in the AXI task.
- [x] `--pr`: `git push -u origin factory/<feature>`, then `gh pr create`.
- [x] Without `--yes`, stop with `awaiting: approval` and print the resume command. Record who approved.
- [x] The PR body holds the acceptance criteria, the gates and the usage totals.
- [x] A secret scan of the changed files before the commit.
- [x] Tests with fakes only. No real push.
- [x] README and test count. Commit.

### AXI: FR-20 to FR-29

- See `docs/code-review.md` section 4.

### Leftovers

- S4 (scaffold types) and the unused `Path` import in `tests/test_runbooks.py`: built. Not committed.
- Still open: C3, S5, S6, S8, the rest of F. See `docs/code-review.md` section 1.

### Impact: `impact-analysis` runbook

Goal: read one or more repos (for example the services of a microservice system) and report which services a new feature or a change would affect.

Decided by the owner (2026-10-31):
- Repos are given by repeating `--repo PATH`. Each folder name is the service name.
- Output is a report, `IMPACT.md`. No code changes.
- Links between services come from reading the code, and from API specs (OpenAPI, AsyncAPI, protobuf, GraphQL) and deploy config (docker-compose, Kubernetes, Helm) when a repo has them.

- Status: committed. The suite passes (313).
- Files: `runbooks/impact-analysis.yaml`, `src/factory/schemas.py` (`service_links`), `src/factory/gates.py` (`link_verified`), `src/factory/runbook.py` (`needs_repos`, `for_each: repos`, `cwd: repo`), `src/factory/engine.py` (repo items, `_check_repo`), `src/factory/cli.py` (repeatable `--repo`, `_read_repos`, `_find_home`), `tests/test_impact.py`, `README.md`.
- Verify: `.venv/bin/pytest -q`, `.venv/bin/factory runbooks`, `.venv/bin/factory plan impact-analysis`. A fake run on two git repos writes `IMPACT.md` and leaves both repos as they were.
- [x] Runbook `runbooks/impact-analysis.yaml`, with `needs_repos` and a `repos` input that takes several `--repo` values.
- [x] Evidence gate `link_verified`: each link cites a file and a line that the engine checks (like `evidence_verified`).
- [x] The repos are read in place. A change or a commit that a reader makes is put back (`_check_repo`).
- [x] Tests with fakes, plus a run on two small fake git repos (`tests/test_impact.py`, 23 tests).
- [x] README and test count. Commit.
