# PRD: Multi-Harness, LLM-Agnostic Software Factory

**Status:** Draft v0.12
**Date:** 2026-10-31
**Owner:** TBD
**Basis:** Design reasoning from a research discussion (Oct 2026). Items marked *Assumption* or *Open question* are not yet validated.

---

## 1. Summary

Build a software factory that turns reviewed specifications into merged pull requests by routing work to interchangeable coding agents ("workers"). Workers use different agent harnesses (for example the Claude Agent SDK, the OpenAI Agents SDK, LangGraph-based agents, or custom loops) and different LLMs. Every worker's output passes the same quality gates, and routing improves over time using measured results.

## 2. Problem Statement

- Agent harness quality varies widely for the same model. Published benchmarks show same-model gaps of roughly 7 to 17 points between harnesses, which exceeds typical model-to-model gains.
- AI-generated code carries more defects and security issues than human-written code in several studies, and functional test pass rate does not predict static-analysis issue density.
- Betting on a single harness or model vendor creates lock-in and survival risk as the ecosystem consolidates.
- Teams lack a fair way to compare harnesses on their own code.

## 3. Goals

| # | Goal | Measure |
|---|------|---------|
| G1 | Swap or add a worker (harness or model) without changing the orchestrator | New worker onboarded in under 2 weeks (*Assumption*) |
| G2 | Apply identical quality gates to every worker | 100% of PRs pass the same gate pipeline |
| G3 | Produce a fair, ongoing comparison of workers on real tasks | Scorecard updated per merged PR |
| G4 | Route tasks to the best-performing worker per task type | Gate first-pass rate improves over baseline |
| G5 | Full traceability of every run | One trace ID spans orchestrator, workers, tools and gateway |
| G6 | An agent can drive the factory without a person reading its output for it | The command line meets the 10 AXI principles (section 8.7) |

## 4. Non-Goals

- Training or fine-tuning models.
- Replacing human review or merge authority.
- Building a general-purpose agent framework. The runbook format serves only the factory's own workflows (see section 6).
- Building a custom LLM gateway if an off-the-shelf one meets requirements.

## 5. Users and Stakeholders

- **Engineers:** write specs, review PRs, handle escalations.
- **Platform/DevOps team:** operate the factory, manage credentials, sandboxes and gateway.
- **Engineering leadership:** use the scorecard for harness and model decisions and for cost control.
- **Security/compliance:** audit access, data handling and change history.

## 6. Scope

### In scope (v1)
- Orchestrator: task intake, routing, retries, budgets, state.
- Worker interface over A2A. A direct-call adapter for workers owned in-house is optional. The saving is too small to measure, because an LLM call takes minutes and an agent starts in seconds.
- At least two workers on different harnesses, one repo.
- Shared quality gate pipeline.
- Scorecard and observability.
- Git-based integration: one branch and one worktree per feature. See section 8.3 and `docs/p10-plan.md`.
- Workflows defined as runbooks (YAML data in `runbooks/`): software builds (`web-app`), research (`research`), code audit (`code-audit`) and API design (`api-design`).
- A triage step that picks the runbook and its traits from a brief, with the user approving the plan before a run starts.
- Worker adapters for the `claude` and `codex` command-line tools, with per-role tool permissions.
- A command line that follows the AXI principles (section 8.7), so that an agent can run the factory.

### Out of scope (v1)
- Cross-organization agent federation.
- Autonomous merge without human approval.

## 7. Architecture Overview

```
Spec + AGENTS.md  ->  Orchestrator (state, routing, retries, budgets)
                          | A2A (or direct call for in-house workers)
        Worker A (Claude Agent SDK)   Worker B (other harness)   Worker C ...
        own sandbox + git worktree    own sandbox + worktree
                          |
        LLM gateway (where the harness allows a base-URL swap)
        MCP tools (repo, CI, tickets, docs)
                          |
        Output: PR  ->  Gates  ->  Human review  ->  Merge

OpenTelemetry trace ID passed through every hop
```

### Layer responsibilities

| Layer | Standard / approach | Responsibility |
|-------|--------------------|----------------|
| Project instructions | AGENTS.md | Per-repo briefing for agents |
| Tool access | MCP | Repo, CI, ticketing, docs |
| Agent-to-agent | A2A | Task dispatch and results between orchestrator and workers |
| Model access | LLM gateway, OpenAI-compatible endpoint | Routing, fallback, cost logs, policy |
| Telemetry | OpenTelemetry GenAI conventions | Traces, token and cost metrics |
| Process | Spec-driven development | Reviewed spec precedes every agent task |

**Design notes**
- A2A does not provide multiple LLMs. Model diversity comes from the harness mix. Each agent is configured to use a `claude` or a `codex` endpoint (question 2).
- A2A does not share a workspace. Git is the integration surface.
- A2A wrapping hides worker internals; the orchestrator sees tasks and results, not tool calls. Use A2A where agents are independently owned, and direct calls where both sides are owned.
- No official A2A wrapper for the Claude Agent SDK was verified. *Open question:* build and test a thin adapter in Phase 1.

## 8. Functional Requirements

### 8.1 Intake and specs
- **FR-1:** A task must reference an approved spec before dispatch.
- **FR-2:** The spec template includes goal, acceptance criteria and constraints. Risk level and affected areas are deferred until a routing table or a PR rule reads them. The run records whether a person or `--yes` approved each checkpoint.
- **FR-3:** The orchestrator rejects tasks with missing or ambiguous acceptance criteria and returns them to the author.

### 8.2 Orchestration and routing
- **FR-4:** Route by rules table (task type, language, risk) in v1.
- **FR-5:** Record every outcome per task type and worker.
- **FR-6:** Shift routing weights from recorded outcomes once sample size is sufficient. *Assumption:* at least 30 tasks per task type before automated shifting. Deferred. A read-only `factory runs` command comes first (runs, first-pass rate, rounds, cost per model).
- **FR-7:** On the last retry of a step, switch to a different allowed worker. Enforce a retry cap. An optional per-task cost cap can apply to one Claude call (`budgets.per_task_usd`). It is not required (owner, 2026-10-31).
- **FR-8:** Allow per-task manual override of the worker.

### 8.3 Worker interface
- **FR-9:** Every worker exposes a standard contract: accept task (spec reference, repo, branch), report status, return a PR or diff plus a run summary. The factory delivers this in 2 slices. Slice 1 makes a worktree on a branch named from the brief and commits the change. It does not push. Slice 2 pushes the branch and opens a pull request, only on an explicit `--pr` request with an approval.
- **FR-10:** Workers publish capabilities (languages, task types, supported models) for routing.
- **FR-11:** Each worker runs in its own sandbox with its own git worktree and scoped, short-lived credentials. The worktree lives in a scratch folder outside the repo, `~/factory-scratch/<repo>/<feature>` (set `FACTORY_SCRATCH` to change the root). The lock is per repo and feature, so several features of one repo run together. *Open question:* the `claude` worker has no OS sandbox. Its limits are the working folder and a tool allowlist.

### 8.4 Quality gates (identical for all workers)
1. Build and tests pass, and new behavior ships with new tests.
2. Static analysis, secret scan and security scan. Next: the secret scan and lint. The security scanner is deferred.
3. Dependency check: packages exist, licenses acceptable, no new unreviewed dependencies. Next: the check that each named package exists. Licences are deferred.
4. Diff-size limit; oversized changes are split. Deferred until a run edits an existing repo.
5. Independent review by a reviewer agent using a different model than the author.
6. Human approval, with stricter rules for high-risk areas.

- **FR-12:** A PR cannot reach human review until gates 1 to 5 pass.
- **FR-13:** Gate configuration is versioned and identical across workers.

### 8.5 Observability and scorecard
- **FR-14:** Propagate one trace ID across orchestrator, worker, tool and gateway calls. Next: a run ID in each request and each log line.
- **FR-15:** Emit OpenTelemetry traces and metrics (tokens, latency, cost, errors). Deferred until an observability backend exists. Every LLM call already logs its model, tokens and cost in `run.json`.
- **FR-16:** Scorecard per worker and task type: first-pass gate rate, rework rounds, issue density, cost per merged PR, cycle time.
- **FR-17:** Redact sensitive data in prompts and responses; apply retention limits. Not required (owner, 2026-10-31; question 4). Deferred. A regex redactor gives false comfort. The secret scan (gate 2) and the clean worker environment cover more.

### 8.6 Gateway
- **FR-18:** Use an off-the-shelf gateway unless a documented requirement forces a custom build. Build only if monthly volume, custom routing or compliance justifies it (*Assumption:* review at roughly 1B tokens per month).
- **FR-19:** Gateway provides provider fallback, rate limiting, per-task cost attribution and prompt/response policy checks.

### 8.7 Agent interface (AXI)

The factory command line has two kinds of user: a person and an agent that runs the factory for a person. These requirements follow the 10 AXI (Agent eXperience Interface) principles at https://axi.md/. Token budget is a design constraint. Each requirement names its principle.

- **FR-20 (AXI 1, token-efficient output):** Every command prints structured data in TOON (Token-Optimized Object Notation) by default. A `--json` flag prints JSON. Prose is for `plan` and for the files that a run saves.
- **FR-21 (AXI 2, minimal default schemas):** List output carries 3 or 4 fields per item. A `--fields` flag adds more. For example, `runbooks` lists name, title and traits only.
- **FR-22 (AXI 3, content truncation):** Long text in output is cut at a set limit and ends with a note of the total size and the flag that shows all of it. A `--full` flag removes the limit. This covers rejection reasons, agent replies and report text.
- **FR-23 (AXI 4, pre-computed aggregates):** Every list and every run status shows totals first. Examples: the number of runbooks, steps accepted and rejected out of the total, and tokens and cost so far.
- **FR-24 (AXI 5, definitive empty states):** A command with no results says so in words, for example `runs: 0 found`. It never prints nothing.
- **FR-25 (AXI 6, structured errors and exit codes):** Errors are structured data on stdout. Stderr carries logs only. Exit code 0 means success. Exit code 1 means the command failed, including a stopped run. Exit code 2 means a bad command or an unknown flag. Commands never prompt. A command that needs a decision from a person returns a state `awaiting: approval` with the command that answers it. Re-running a command on finished work changes nothing.
- **FR-26 (AXI 7, ambient context):** A setup command installs a session hook and an installable Agent Skill. Each session then starts with a short summary of runs in the current directory: name, status and cost.
- **FR-27 (AXI 8, content first):** Running `factory` with no arguments prints live data and not help text. The data is the executable path (with `~` for the home directory), a 1-sentence description, the runs in the current directory and the runbooks.
- **FR-28 (AXI 9, contextual disclosure):** Every command ends with `help[]` lines. Each line is a command template for a likely next step. Fixed flags stay. Runtime values stay as placeholders such as `<run>`.
- **FR-29 (AXI 10, consistent help):** Every subcommand has a short `--help` with 1 example.
- **FR-30 (cost of every call):** Every call reports its cost. A worker whose provider does not report cost (Codex today) gets its cost from a price table, `docs/model-prices.md`, per model and per token type (input, cached input, output). The table takes its prices from https://www.llm-prices.com/ (feed `current-v1.json`) and is refreshed weekly by the skill `update-model-prices`. An alias takes the price of its target model: `gpt-advantage-assist` is `gpt-6.1-sol`, and `claude-advantage-assist` is `claude-opus-5-5` (owner, 2026-10-31). A call with no price in the table reports its cost as unknown, and the run total says how many calls are unknown. The table and each refresh carry a date. Requirement added by the owner on 2026-10-31. Built.

## 9. Non-Functional Requirements

- **Security:** Workers are non-human identities with least-privilege, short-lived credentials. No worker can push to protected branches. Agents running locally must not bypass the factory's tool controls.
- **Isolation:** Sandboxed execution per task; no shared mutable state except git.
- **Portability:** Orchestrator must not depend on any single vendor SDK. Vendor-specific code lives only in worker adapters.
- **Reliability:** Failed or hung workers time out and release resources; tasks are resumable or restartable. Next: `factory run --resume` skips the steps that `run.json` records as accepted.
- **Cost control:** Every LLM call logs its model, tokens and cost. The owner chose logging only for now. An optional per-task cap covers one Claude call. It is not required (owner, 2026-10-31). Codex cost comes from the price table (FR-30). A budget for a run or a day, with a hard stop, is deferred.
- **Auditability:** Every PR links to its spec, trace, worker, model and gate results.
- **Cross-platform:** Workers run on Linux and macOS. Windows is deferred (decided, question 3). The code is Unix-only today (`fcntl` lock, `.venv/bin/...` paths). Tracked as P18.

## 10. Success Metrics

| Metric | Baseline | Target |
|--------|----------|--------|
| First-pass gate rate | Measure in Phase 2 | Improve over baseline each quarter |
| Rework rounds per PR | Measure in Phase 2 | Decrease |
| Static-analysis issue density | Measure in Phase 2 | At or below human-written baseline |
| Cost per merged PR | Measure in Phase 2 | Within agreed budget |
| Time to onboard a new worker | n/a | Under 2 weeks (*Assumption*) |
| Escaped defects from agent PRs | Measure | At or below human baseline |

Targets are placeholders until baselines exist.

## 11. Phased Delivery

| Phase | Deliverable | Exit criteria |
|-------|-------------|---------------|
| 1 | Orchestrator plus one worker, spec intake, git worktree flow (slice 1: commit on a branch; slice 2: PR) | End-to-end spec to PR on one repo |
| 2 | Gate pipeline and scorecard | All six gates enforced; baselines captured |
| 3 | Second worker (different harness) behind A2A, with tracing across the boundary | Same gates; side-by-side scorecard |
| 4 | Gateway for model routing and cost attribution | Model swap is a config change |
| 5 | Data-driven routing and retry escalation | Routing weights adjust from outcomes |
| 6 | Evaluate additional workers and further non-code workflows beyond the four runbooks in section 6 | Go/no-go decision |

## 12. Validation Plan (head-to-head benchmark)

No public benchmark compares these setups on your code. Run your own:
1. Select 30 to 50 real tickets from the target repo.
2. Run the same model through each candidate harness, with fixed seeds and three runs each.
3. Compare first-pass gate rate, static-analysis issue density, review rework time and cost per task.
4. Repeat with a second model to separate harness effects from model effects.

## 13. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Harness or SDK vendor changes terms, pricing or direction | Lock-in, cost shock | Adapter boundary; at least two harnesses live |
| Benchmark claims mislead selection | Wrong harness chosen | Use own-repo validation plan |
| AI code has more defects and security issues | Quality and security incidents | Uniform gates, independent reviewer model, human approval |
| Hallucinated dependencies | Supply-chain risk | Dependency existence and license gate |
| A2A overhead or immaturity for in-house workers | Slower delivery | Measured as small: an agent starts in seconds and an LLM call takes minutes. Direct calls stay optional. |
| A `claude` worker writes outside its worktree | Changes reach the original checkout | Check `git status` after each step and revert changes outside the worktree. Confirm what the `claude` CLI allows outside its working folder. |
| Telemetry conventions still changing | Re-instrumentation | Adopt OpenTelemetry GenAI conventions now, isolate in one module |
| Custom components replaced by better off-the-shelf tools | Wasted effort | Prefer existing gateway and tracing tools |
| Review bottleneck as agent output grows | Slower merges | Diff-size limits, reviewer agent, risk-based review rules |
| Prompt or response data leakage | Compliance breach | Redaction, retention limits, scoped data access |

## 14. Dependencies

- Source control platform with branch and worktree support.
- CI system able to run gates on every PR.
- Sandboxing or container runtime for workers.
- LLM gateway and provider accounts.
- Observability backend that ingests OpenTelemetry.
- Secrets management for short-lived credentials.

## 15. Open Questions

1. **Decided by the owner (2026-10-31).** The first workers are the `claude` and `codex` harnesses, with the models in `llm.yaml`. The worker layer must be a plugin design, so a new harness or use case needs no change to the engine. The owner chose a module path that `llm.yaml` names, loaded at start, not a package entry point. Tracked as P19.
2. **Decided by the owner (2026-10-31).** The endpoint is not overridden. Each agent is configured to use a `claude` or a `codex` endpoint, and the owner has provided both in `llm.yaml`. The live run is still not done.
3. **Decided by the owner (2026-10-31): no Windows support for now.** Workers run on Linux and macOS. Revisit later; the Unix-only code is listed in `docs/code-review.md`, item P18.
4. **Decided by the owner (2026-10-31).** The gateway may receive the code and prompts. Redaction is not required.
5. What are the budget caps per task and per day? Deferred. Cost is logged only.
6. **Not required for now (owner, 2026-10-31).** No pilot repository or risk tier is chosen.
7. **Decided by the owner (2026-10-31).** The owner of the factory owns the scorecard and the routing decisions (`agents.yaml`).
8. **Resolved in v0.4.** A checkpoint needs a person, and FR-25 says commands never prompt. A run that reaches a checkpoint stops and saves its state as `awaiting: approval`, with the command that answers it, such as `factory approve <run>`. An agent can approve a checkpoint with `--yes`, which is an explicit choice. The run must record whether a person or `--yes` approved each checkpoint. Built in v0.12: `factory approve --name <run>` and the `approvals` field of `run.json`.
9. **Decided by the owner (2026-10-31), not built.** Triage can answer "none" with a reason, and the run stops before any work. The user sees a disclaimer that there is no runbook for the request. Before this, triage had to choose a runbook, and a choice outside the catalog was rejected. A catch-all runbook would hide the gap.

## 16. Assumptions

- The first release targets code tasks on a single repository.
- Humans retain merge authority.
- An off-the-shelf gateway and observability backend are acceptable.
- Numeric targets above are placeholders pending Phase 2 baselines.

## 17. Revision History

| Version | Date | Change |
|---------|------|--------|
| v0.1 | 2026-10-28 | First draft. |
| v0.2 | 2026-10-31 | Non-code workflows, the runbook format, triage and the command-line worker adapters moved into v1 scope (section 6). Non-goals in section 4 updated to match. Phase 6 narrowed to what is still undecided. Status and implementation gaps are tracked in `docs/code-review.md`, not here. |
| v0.3 | 2026-10-31 | Added goal G6, the scope item and section 8.7 (FR-20 to FR-29): the command line follows the 10 AXI principles. Added open question 8. |
| v0.4 | 2026-10-31 | Resolved open question 8: a run waits at a checkpoint with `awaiting: approval`, and `--yes` is the explicit approval for an agent. |
| v0.12 | 2026-11-01 | FR-25 built, except structured errors on stdout: no command prompts, `factory approve`, the approver record and exit codes 0, 1 and 2. |
| v0.11 | 2026-10-31 | FR-30 built: the price table is `docs/model-prices.md`, from llm-prices.com, refreshed weekly by a skill. Aliases set by the owner. |
| v0.10 | 2026-10-31 | Added FR-30: a price table gives the cost of calls whose provider reports no cost (Codex). Owner request. |
| v0.9 | 2026-10-31 | Question 3 changed: no Windows support for now (NFR cross-platform, P18 deferred). Question 1: the plugin is a module path named in `llm.yaml` (P19). |
| v0.8 | 2026-10-31 | Recorded the owner answers to questions 1, 2, 4, 6 and 7. Question 1 requires a plugin design for workers (P19). Redaction is not required (FR-17). Question 5 stays deferred. |
| v0.7 | 2026-10-31 | Recorded the owner decision on question 3: workers must run on Windows as well as Linux and macOS (NFR cross-platform, question 3). Added backlog item P18 in `docs/code-review.md`. |
| v0.6 | 2026-10-31 | Recorded the owner decision on question 9: triage can answer "none" with a reason, and the run stops before any work. The user sees a disclaimer that there is no runbook for the request. Not built. Tracked as P17 in `docs/code-review.md` and as issue 4 on GitHub. |
| v0.5 | 2026-10-31 | Moved to `docs/`. Recorded the owner decisions on the gaps: cost is logged only (NFR cost control, question 5); the real-repo flow in 2 slices with a scratch worktree (FR-9, FR-11); failover on the last retry (FR-7); the direct-call adapter is optional; risk fields, OpenTelemetry, redaction, weight shifting, the security scanner, licences and diff-size are deferred. Added open question 9 and a risk for the `claude` worker. |

## 18. Evidence Notes

- Same-model score gaps across harnesses and weaker evidence for open-framework gaps come from benchmark papers and secondary analyses; treat as directional.
- Defect and security statistics for AI-generated code come from a mix of academic and vendor studies.
- A2A and MCP are hosted under the Linux Foundation's Agentic AI Foundation (the exact date and path of A2A's move was reported inconsistently across sources).
- Star counts and download figures vary across sources and include CI and transitive installs; they indicate activity, not user counts.
