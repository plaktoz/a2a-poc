# Code review: `plaktoz/feat-thinkbig`

Date: 2026-10-31

This file lists only the open findings and actions. The standards baseline is the Fowler smell list. The spec source is `docs/software-factory-prd.md` (now v0.9). The IDs match the first review. `docs/handoff.md` and `docs/p10-plan.md` refer to them.

## 1. Open findings

| # | Finding | Recommended action |
|---|---|---|
| S3 | `engine.py` still branches on output-name strings. A new schema touches `render` and the summary code. | Action F (rest): one record per schema. Defer until a ninth schema is needed. |
| S4 | `Template.suite` and `Template.serve` are bare strings (`scaffold.py:41,42,63`). | Use `Literal`. Optional. Low value without a type checker. |
| S5 | `worker, model` travel together. `Engine.__init__` takes many injected callables. | Defer. Bundle them when the next change touches both. |
| S6 | The `vitest` and `pytest` gates share one skeleton (`gates.py`). | Extract one helper. Small. |
| S8 | `_review_test_change` (`engine.py:435`) embeds a long prompt and works mostly on `Template` fields. | Move the prompt to `roles.yaml` or the runbook. Defer the `engine.py` split (633 lines). |
| P3, P4 | `per_task_usd` caps one Claude call only. `per_day_usd` is parsed and never read. | Action C3: keep `per_day_usd` and document it as unused. Add a budget later (see action D). |
| Lint | `tests/test_runbooks.py` has an unused `Path` import. It is old. | Delete the import. |

## 2. Open actions

| Action | Status | What remains |
|---|---|---|
| C | C1 is done. C3 is open. | Keep `per_day_usd` and document it as unused. `reviewer.candidates` stays because the config validates it. |
| D | Logging is done. | No budget, by owner decision. To add one, read `usage.cost_usd` in `run.json` after each step, then stop at a limit. Codex cost needs a price table. |
| F | The `SCHEMAS` merge is done. | The rest (S3) is deferred. |
| J | Open. | P5 to P13 and P17 to P19 below. Not one change. Build in the order in section 5. |

## 3. Action J: PRD gaps (P5 to P13 and P17 to P19)

Method: state the outcome the requirement protects, then pick the cheapest change that protects it. Action D is done, so items that needed the usage log can start.

| # | PRD requirement | What is missing | Decision or advice | Size |
|---|---|---|---|---|
| P5 | FR-7 failover to a different worker | A retry goes to the same worker. | Do. On the last retry of a step, switch to another allowed worker. Keep the independence check true. Count the cost of both workers. | Medium |
| P6 | Gates 2, 3, 4, 6 | No secret scan, lint or package check. Gate 1 is partial. | Do 3 of 6: secret scan, lint and package check. The secret scan is a regex over written files. Lint uses the scaffold commands. Check that each named npm or PyPI package exists. Defer the security scanner, licences and diff-size. Gate 6 stays the `checkpoint`. | Medium |
| P7 | FR-13 gate versioning | Gate configuration is not versioned. | Do the small part. Write the git commit and the gate names of each step into `run.json`. | Small |
| P8 | FR-1 to FR-3 risk level | The `Spec` schema has no risk fields. | Do not add risk fields. They have no reader. Record in `run.json` whether a person or `--yes` approved each checkpoint. | Small |
| P9 | FR-5, 6, 8, 16 outcome store | No cross-run view. Only `--staff` override. | Do the read-only part: a `factory runs` command that sums `apps/*/.factory/run.json` (runs, first-pass rate, rounds, cost per model). Defer weight shifting until 30 tasks per type exist. | Medium |
| P10 | FR-9, FR-10 real repo | No repo or branch input, no diff or PR output, no worktree. | Decided: build it. The plan is in `docs/p10-plan.md`. Two slices: worktree with a commit, then PR creation. | Large |
| P11 | FR-14, 15, 17 tracing | No shared run ID. | Do the cheap part: a run ID in each request and each log line. Defer OpenTelemetry and redaction. | Small |
| P12 | Reliability: resume | A second run needs `--rebuild` and starts again. | Do. `factory run --resume` skips each step and item that `run.json` records as accepted. It reads output from `artifacts.json`. The command must be idempotent (AXI 6). | Medium |
| P13 | A2A direct-call adapter | None. | Do not build. The saving is too small. Change the PRD to say direct calls are optional. | None |
| P17 | PRD question 9: a request that fits no runbook | Triage must choose a runbook. `_check_triage` (`schemas.py:215`) rejects a name outside the catalog. `roles.yaml` says "Choose exactly one runbook." | Decided by the owner. Issue: https://github.com/plaktoz/a2a-poc/issues/4. Triage can answer `none` with a reason. The CLI prints a disclaimer that there is no runbook for this request, with the reason. It makes no workspace and exits with code 1. Change the triage prompt and `_check_triage`. Add tests: a request that fits no runbook, and a request that still picks one. A catch-all runbook would hide the gap. | Small |
| P19 | PRD question 1: a plugin design for workers | Decided by the owner: the worker layer is a plugin design. Today `invoke()` (`invoke.py:50`) branches on `harness` with `if`/`elif`. Adding a harness means editing the engine. The config check accepts only `codex` and `claude-agent-sdk` (`invoke.py:74`). | Define a worker plugin interface: a name, a `run(prompt, cwd, tools, model)` method that returns `(text, Usage)`, and a `validate` method for the config. Decided by the owner (2026-10-31): `llm.yaml` names each worker's plugin by module path, loaded at start. Not a package entry point. Move `claude` and `codex` into plugins. The engine and runbooks must not change. Test with a fake plugin. Do this before P10 slice 1, so P10 uses the interface. | Medium |
| P18 | PRD question 3 and the cross-platform NFR: Windows | Deferred by the owner (2026-10-31): no Windows support for now. Unix-only code: `fcntl.flock` in `engine.py:18,65`; `.venv/bin/pytest` and `.venv/bin/uvicorn` in `gates.py:98,208`; `templates/fastapi.yaml:6`; the `finish` text in `runbooks/web-app.yaml`. Not checked: `npx`, the `codex` sandbox and the `claude` CLI on Windows. | Replace `fcntl` with a lock that works on both systems. Find the venv folder (`Scripts` or `bin`) in one helper. Run the suite on a Windows host. P10 paths and `git worktree` need the same check. | Medium |

## 4. AXI gaps (FR-20 to FR-29)

AXI means the 10 principles at https://axi.md/. They are FR-20 to FR-29 in the PRD. None is built.

| Requirement | Gap in the code | Related |
|---|---|---|
| FR-25: never prompt, exit code 1 on a stopped run, errors as data | `cli.py:109` asks "Run this plan?". Checkpoints ask on the terminal. Errors go to stderr. `FactoryError` exits with 2, and AXI keeps 2 for a bad command. | P8, P12 |
| FR-20 to FR-24: TOON output, small default fields, truncation, totals, empty states | All output is prose. | P9 |
| FR-26 to FR-29: ambient context, content first, `help[]` lines, help | `factory` with no arguments prints an argparse error. | P9 |

FR-25 and checkpoints are decided in PRD v0.4 (question 8). A run waits at a checkpoint with `awaiting: approval`. The `--yes` flag is the explicit agent approval. The run records who approved. The `factory approve` command and the approver record are not built.

## 5. Suggested build order

1. P7 and P11. Small, no dependencies.
2. P17, P5, P12, P9 and P6. P18 (Windows) is deferred.
3. AXI work, with the `factory approve` command and the approver record.
4. P19 (worker plugins) before P10 slice 1. Then P10, following `docs/p10-plan.md`. Build the P6 secret scan before slice 2.
5. Leftovers: C3, S4, S5, S6, S8, the rest of F, and the unused `Path` import.
6. Do not build P13. Do not add risk fields (P8).

## 6. Done

| ID | Why it is gone |
|---|---|
| A, B (P1, P2) | The docs state what the code does. The independence check now covers `reviewer` and `architect`. |
| C1 | `reviewer_for()` and its tests are deleted. |
| D (P3) | Every LLM call logs model, tokens and cost. Logging only, by owner decision. |
| E (S1, S2, S9) | One `APP_NAME` rule. `GateCtx.item` removed. Unused test imports removed. |
| E (S6 partly) | One `Json` alias and one `_dump` helper. The gate skeleton stays open. |
| F (first step) | `MODELS`, `SHAPES` and `_CHECKS` are one `SCHEMAS` dict. |
| G (S4 partly) | Failure levels use `Level = Literal[...]` in `gates.py`. |
| H (P14, P15, P16) | The PRD records that non-code workflows, the runbook DSL and the CLI wrappers are in scope. |
| I | A stopped `research` run marks its draft "NOT APPROVED" and exits with code 1. |
| K (S7) | Renamed `_brief`, `_problem` and `_lines`. The "Gate 5" docstring is fixed. |
| S10 | Judged acceptable. No action. |
| Open questions | Answered by B, I and H. |
