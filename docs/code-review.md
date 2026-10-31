# Code review: `plaktoz/feat-thinkbig`

Date: 2026-10-31

## 1. Scope

| Item | Value |
|---|---|
| Fixed point | `origin/main` (merge-base `31d1264`) |
| Commits | `2a62022`, `9f83917`, `89a8428`, `ed635ad` |
| Diff size | 48 files |
| Standards source | None. The repo documents no coding standards, so only the Fowler smell baseline applied. Every Standards finding is a judgement call. |
| Spec source | `software-factory-prd.md` |

Two sub-agents reviewed the diff in parallel. The two axes are not merged and not ranked against each other.

Limits of this review:

- Line numbers come from the sub-agents. I did not recheck each one by hand.
- I changed no code and no documents when I ran the review.
- The live run (level 3) was not run.
- No person has checked `docs/README.html`.

## 2. Standards findings (10)

| # | Smell | Where | Finding |
|---|---|---|---|
| S1 | Duplicated Code, Primitive Obsession | `engine.py:39`, `schemas.py:265` | The app-name regex `[a-z0-9][a-z0-9-]{0,39}` exists twice. Similar ID patterns are split across `runbook.py:33` and `roles.py:34`. |
| S2 | Speculative Generality | `gates.py:58`, `engine.py:319,426`, `schemas.py:230` | `GateCtx.item` is filled but never read. `_context(ref, item)` ignores `item`. The inner `check(data, ctx)` ignores `ctx`. Some gates ignore one of `(ctx, out)`. This last point is arguable, because the signature is uniform on purpose. |
| S3 | Repeated Switches, Shotgun Surgery | `engine.py:330,332,482,508-512,528`, `schemas.py:284-339` | The code branches on output-name strings in many places. Adding a schema touches `MODELS`, `SHAPES`, `_CHECKS`, `render` and the summary code. |
| S4 | Primitive Obsession | `gates.py:34`, `engine.py:218,281,283`, `scaffold.py:63` | Failure levels and `Template.suite` and `Template.serve` are bare strings, checked by hand. |
| S5 | Data Clumps | `invoke.py:43`, `agent.py:91,106,119`, `engine.py:150-156` | `worker, model` travel together, although `Staff` already bundles them. `Engine.__init__` takes many injected callables. |
| S6 | Duplicated Code (minor) | `engine.py:136,485,489`, `gates.py:73-116`, 3 files | `json.dumps(..., indent=2) + "\n"` appears 3 times. The `vitest` and `pytest` gates share one skeleton. `Json = dict` is defined 3 times. |
| S7 | Mysterious Name | `engine.py:518,526`, `gates.py:134`, `runbook.py:366` | `_brief`, `_problem` and `_lines` do not say what they do. The docstring says "Gate 5", which nothing in `src/` defines. |
| S8 | Feature Envy (weak) | `engine.py:359-421` | `_check_files` and `_review_test_change` work mostly on `Template` fields. The second embeds a long prompt. |
| S9 | Unused imports | `tests/test_engine.py:7,13` | `yaml` and `Runbook` are imported and not used. |
| S10 | Broad `except` | `fleet.py:62`, `search.py:104` | Judged acceptable. One re-raises on timeout. The other records the error. |

Not found: commented-out code, Middle Man (`_LazyRoles` is justified), Refused Bequest. Divergent Change is borderline: `engine.py` (551 lines) holds the lock, snapshots, the run log, the engine, triage and console output.

## 3. Spec findings (16)

### Implemented but wrong (3)

| # | PRD line | Finding |
|---|---|---|
| P1 | §8.4 gate 5: independent review by a different model | `_check_independence` (`runbook.py:365`) compares only reviewer/writer and verifier/developer. `api-design` has an `architect` and a `reviewer` but no `writer`, so the check is skipped. `docs/README.md:26` says the factory refuses such a plan. That is false for this runbook. |
| P2 | Reviewer chosen from `reviewer.candidates` | `Config.reviewer_for()` (`config.py:212`) is called only from tests. `docs/README.md:121` and `:331` present it as live. |
| P3 | §9 and FR-7: per-task budget with hard stop | `per_task_usd` is passed only as `--max-budget-usd` to each `claude -p` call (`invoke.py:59`). It is a per-call cap. Codex calls have no cap. Nothing sums spend across a run. |

### Missing or partial (10)

| # | Item | State |
|---|---|---|
| P4 | Per-day budget | `Budgets.per_day_usd` is parsed and never enforced. |
| P5 | FR-7 failover to a different worker | Retries go back to the same worker, with feedback. |
| P6 | Gates 2, 3, 4, 6 | No security scan, no dependency or licence check, no diff-size limit. Gate 1 is partial. Human approval is only an optional `checkpoint`. |
| P7 | FR-13 | Gate configuration is not versioned. |
| P8 | FR-1 to FR-3 | The `Spec` schema has no risk level or affected areas. Nothing checks that a spec is approved before dispatch. |
| P9 | FR-5, 6, 8, 16 | No cross-run outcome store, scorecard or weight shifting. Only manual `--staff` override exists. |
| P10 | FR-9, FR-10 | No repo or branch input, no PR or diff output, no git worktree per task. |
| P11 | FR-14, 15, 17 | No trace IDs, no OpenTelemetry, no redaction. |
| P12 | Reliability | Per-call timeouts exist. There is no resume. |
| P13 | A2A adapter | There is no direct-call adapter. |

### Not asked for by the PRD (3)

| # | Item | Note |
|---|---|---|
| P14 | Non-code workflows (`research`, `api-design`, web search, citation checks) | The PRD lists them as a non-goal. You asked for them, so the PRD is outdated. |
| P15 | Runbook DSL, triage role, scaffold templates, web app builder | This is close to the non-goal "general-purpose agent framework". Also an outdated PRD. |
| P16 | Codex and Gemini CLI wrappers | These act as a routing layer, which is close to the non-goal "custom LLM gateway". |

## 4. Recommended actions

Order: do the cheap fixes that remove wrong statements first. Then decide the larger items.

### A. Fix the documentation overstatements (P1, P2) — recommended now

Change `docs/README.md:26`, `:121` and `:331` so they state what the code does. Name the pairs that the independence check covers. State that `reviewer_for` and `reviewer.candidates` are not used at runtime.

| Pros | Cons |
|---|---|
| Takes minutes. | Documents a gap instead of closing it. |
| Removes the only false statements the review found. | Must be repeated in `docs/README.html`, so the two files stay equal. |
| No risk to behaviour. | |

### B. Extend the independence check to `architect` and `reviewer` (P1) — recommended

Add the pair to `_check_independence` and add a test with `api-design`.

| Pros | Cons |
|---|---|
| Closes a real gap against PRD §8.4 gate 5. | Changes behaviour. A plan that passed before can now be refused. |
| Small change, one test. | A user with one enabled worker can no longer run `api-design` without `--staff` changes. |
| Makes the README statement true. | |

If you do B, A needs only the `reviewer_for` part.

### C. Decide the fate of `reviewer_for` and `per_day_usd` (P2, P4)

| Option | Pros | Cons |
|---|---|---|
| C1. Delete both and their config keys | Removes dead code and a misleading config surface. Smaller docs. | Loses the hook for a planned feature. Changes `llm.yaml` shape and tests. |
| C2. Wire them in | Matches the PRD. The reviewer choice becomes data-driven. | Needs design: how it interacts with `agents.yaml` and `--staff`. More tests. Larger change. |
| C3. Keep and document as unused | No code change. | The dead code stays. A reader must keep this in mind. |

Recommendation: C1 for `reviewer_for`, C3 for `per_day_usd` until a cost total exists (see D).

### D. Add a run-level cost total (P3)

Sum spend per run and stop at `per_task_usd`.

| Pros | Cons |
|---|---|
| Makes the budget a real hard stop. | Codex calls may not report cost, so the total would be partial. |
| Gives `per_day_usd` something to build on. | Needs a cost field in the A2A reply and in `run.json`. |
| | Medium size. Touches `invoke.py`, `agent.py` and `engine.py`. |

Alternative: rename the setting to `per_call_usd` and document it. This is cheap but breaks existing `llm.yaml` files.

### E. Remove small dead code and duplicates (S1, S2, S6 partly, S9)

Export one app-name constant. Delete `GateCtx.item` and the unused `item` parameter. Remove the two unused test imports. Share one `Json` alias and one JSON-writing helper.

| Pros | Cons |
|---|---|
| Low risk and mostly mechanical. | Touches many files for a small gain. |
| Removes drift risk for the app-name rule. | Merge conflicts if other branches edit `engine.py` or `gates.py`. |
| Tests already cover the paths (125 pass). | |

Do not remove the unused `(ctx, out)` arguments in gates. The uniform signature is deliberate.

### F. Replace output-name branching with one per-schema record (S3)

One record per schema holds model, shape, check, render and summary.

| Pros | Cons |
|---|---|
| Adding a schema becomes a one-place change. | Largest refactor in this list. Touches `engine.py` and `schemas.py`. |
| Removes Repeated Switches and Shotgun Surgery together. | There are only 8 schemas today, so the gain is modest. |
| | Risk of subtle changes to output text that tests check. |

Recommendation: defer until a ninth schema is needed.

### G. Use `Literal` or an enum for failure levels (S4)

| Pros | Cons |
|---|---|
| A typo fails at load time and in the type checker. | The repo has no type checker configured, so the gain is smaller. |
| Small change. | Runbook YAML still carries strings, so a conversion step is still needed. |

Recommendation: do it together with E if you want it. It is optional.

### H. Update `software-factory-prd.md` (P14, P15, P16)

Record that non-code workflows, the runbook DSL, triage and the CLI wrappers are now in scope.

| Pros | Cons |
|---|---|
| The next Spec review compares against the real intent. | Costs writing time, and the PRD owner must agree. |
| Clears 3 of the 16 Spec findings. | A PRD that follows the code can hide scope decisions. |

Recommendation: do it. Otherwise each review repeats the same 3 findings.

### I. Decide how `research` ends when the reviewer never approves

This question is still open from earlier. Today the run ends as `stopped` and leaves the draft on disk.

| Option | Pros | Cons |
|---|---|---|
| I1. Keep `stopped` | Honest. No unreviewed report is called a result. | The user gets no deliverable. |
| I2. Save the last draft marked "not approved" | The user keeps the work. | A reader may trust an unreviewed report. Needs a clear marker. |
| I3. Ask the user at the end (interactive only) | The user decides. | Does not work with `--yes`. |

Recommendation: I2 with a visible banner in the file and a non-zero exit code.

### J. Implement missing PRD features (P5–P13)

Not recommended in this branch. Each item is a feature of its own. Open one issue per item, and start with FR-7 failover (P5) and resume (P12), which have the clearest user value.

| Pros | Cons |
|---|---|
| Moves the factory toward the PRD. | Large. Several items need design (outcome store, tracing, git worktrees). |
| | The PRD itself may change after action H. |

### K. Smaller naming and structure items (S5, S7, S8)

Rename `_brief`, `_problem` and `_lines`. Fix the "Gate 5" docstring. Consider moving the prompt in `_review_test_change` into `roles.yaml` or the runbook. Consider splitting `engine.py`.

| Pros | Cons |
|---|---|
| Easier to read. | Splitting `engine.py` is a big change for a judgement-call smell. |
| The prompt move fits the "roles as data" direction. | Renames add noise to `git blame`. |

Recommendation: do the renames and the docstring. Defer the file split.

## 5. Suggested order

| Step | Actions | Size |
|---|---|---|
| 1 | A, B | Small. One commit. Done: the reviewer now differs from the architect too, with a test. |
| 2 | E, G, K (renames only) | Small. One commit. Done, except the items listed under "Step 2 status" below. |
| 3 | C1 for `reviewer_for`, H | Small. C1 is done: `reviewer_for()` and its 2 tests are deleted. `reviewer.candidates` stays, because the config still validates it at load time. H is open. |
| 4 | I2 | Small to medium. |
| 5 | D | Medium. Own branch. |
| 6 | F, J | Large. Defer and track as issues. |

### Step 2 status

Done:

- S1: one `APP_NAME` rule, in `schemas.py`. `engine.py` uses it.
- S2: removed `GateCtx.item` and the unused `item` parameter of `_context`. The uniform `(ctx, out)` gate signature stays on purpose.
- S4 (partly): failure levels use `Literal["step", "round", "run"]` (`Level` in `gates.py`).
- S6 (partly): one `Json` alias (`a2a_transport.py`) and one `_dump` helper for JSON files.
- S7: `_brief` is now `_summary_suffix`, `_problem` is `_console_reason`, `_lines` is `_capped`. The "Gate 5" wording is now "Independence".
- S9: removed the 2 unused imports in `tests/test_engine.py`.

Not done:

- S4: `Template.suite` and `Template.serve` are still strings.
- S5, S8: `worker, model` clump, long `Engine.__init__`, and the prompt in `_review_test_change`.
- S6: the shared skeleton of the `vitest` and `pytest` gates.
- S3: the per-schema record (action F), deferred.
- Ruff reports one unused `Path` import in `tests/test_runbooks.py`. It was not in the review.

## 6. Open questions

- Should `api-design` refuse a plan where the architect and the reviewer share a model (action B)?
- How should `research` end when the reviewer never approves (action I)?
- Who owns `software-factory-prd.md`, and may it be updated (action H)?
