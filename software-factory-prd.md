# PRD: Multi-Harness, LLM-Agnostic Software Factory

**Status:** Draft v0.1
**Date:** 2026-10-28
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

## 4. Non-Goals

- Training or fine-tuning models.
- Replacing human review or merge authority.
- Building a general-purpose agent framework.
- Supporting non-code tasks in v1 (deferred to a later phase).
- Building a custom LLM gateway if an off-the-shelf one meets requirements.

## 5. Users and Stakeholders

- **Engineers:** write specs, review PRs, handle escalations.
- **Platform/DevOps team:** operate the factory, manage credentials, sandboxes and gateway.
- **Engineering leadership:** use the scorecard for harness and model decisions and for cost control.
- **Security/compliance:** audit access, data handling and change history.

## 6. Scope

### In scope (v1)
- Orchestrator: task intake, routing, retries, budgets, state.
- Worker interface over A2A, plus a direct-call adapter for workers owned in-house.
- At least two workers on different harnesses, one repo.
- Shared quality gate pipeline.
- Scorecard and observability.
- Git-based integration: one branch or worktree per task.

### Out of scope (v1)
- Cross-organization agent federation.
- Autonomous merge without human approval.
- Non-code workflows.

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
- A2A does not provide multiple LLMs. Model diversity comes from the harness mix plus the gateway for harnesses that permit a base-URL swap. *Open question:* confirm per harness whether the endpoint can be redirected.
- A2A does not share a workspace. Git is the integration surface.
- A2A wrapping hides worker internals; the orchestrator sees tasks and results, not tool calls. Use A2A where agents are independently owned, and direct calls where both sides are owned.
- No official A2A wrapper for the Claude Agent SDK was verified. *Open question:* build and test a thin adapter in Phase 1.

## 8. Functional Requirements

### 8.1 Intake and specs
- **FR-1:** A task must reference an approved spec before dispatch.
- **FR-2:** The spec template includes goal, acceptance criteria, constraints, risk level and affected areas.
- **FR-3:** The orchestrator rejects tasks with missing or ambiguous acceptance criteria and returns them to the author.

### 8.2 Orchestration and routing
- **FR-4:** Route by rules table (task type, language, risk) in v1.
- **FR-5:** Record every outcome per task type and worker.
- **FR-6:** Shift routing weights from recorded outcomes once sample size is sufficient. *Assumption:* at least 30 tasks per task type before automated shifting.
- **FR-7:** If a worker fails gates twice, retry once with a different worker. Enforce a retry cap and a per-task cost cap.
- **FR-8:** Allow per-task manual override of the worker.

### 8.3 Worker interface
- **FR-9:** Every worker exposes a standard contract: accept task (spec reference, repo, branch), report status, return a PR or diff plus a run summary.
- **FR-10:** Workers publish capabilities (languages, task types, supported models) for routing.
- **FR-11:** Each worker runs in its own sandbox with its own git worktree and scoped, short-lived credentials.

### 8.4 Quality gates (identical for all workers)
1. Build and tests pass, and new behavior ships with new tests.
2. Static analysis, secret scan and security scan.
3. Dependency check: packages exist, licenses acceptable, no new unreviewed dependencies.
4. Diff-size limit; oversized changes are split.
5. Independent review by a reviewer agent using a different model than the author.
6. Human approval, with stricter rules for high-risk areas.

- **FR-12:** A PR cannot reach human review until gates 1 to 5 pass.
- **FR-13:** Gate configuration is versioned and identical across workers.

### 8.5 Observability and scorecard
- **FR-14:** Propagate one trace ID across orchestrator, worker, tool and gateway calls.
- **FR-15:** Emit OpenTelemetry traces and metrics (tokens, latency, cost, errors).
- **FR-16:** Scorecard per worker and task type: first-pass gate rate, rework rounds, issue density, cost per merged PR, cycle time.
- **FR-17:** Redact sensitive data in prompts and responses; apply retention limits.

### 8.6 Gateway
- **FR-18:** Use an off-the-shelf gateway unless a documented requirement forces a custom build. Build only if monthly volume, custom routing or compliance justifies it (*Assumption:* review at roughly 1B tokens per month).
- **FR-19:** Gateway provides provider fallback, rate limiting, per-task cost attribution and prompt/response policy checks.

## 9. Non-Functional Requirements

- **Security:** Workers are non-human identities with least-privilege, short-lived credentials. No worker can push to protected branches. Agents running locally must not bypass the factory's tool controls.
- **Isolation:** Sandboxed execution per task; no shared mutable state except git.
- **Portability:** Orchestrator must not depend on any single vendor SDK. Vendor-specific code lives only in worker adapters.
- **Reliability:** Failed or hung workers time out and release resources; tasks are resumable or restartable.
- **Cost control:** Per-task and per-day budgets with hard stops.
- **Auditability:** Every PR links to its spec, trace, worker, model and gate results.
- **Cross-platform:** *Open question:* confirm whether workers must run on Windows hosts as well as Linux.

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
| 1 | Orchestrator plus one worker (direct call), spec intake, git worktree flow | End-to-end spec to PR on one repo |
| 2 | Gate pipeline and scorecard | All six gates enforced; baselines captured |
| 3 | Second worker (different harness) behind A2A, with tracing across the boundary | Same gates; side-by-side scorecard |
| 4 | Gateway for model routing and cost attribution | Model swap is a config change |
| 5 | Data-driven routing and retry escalation | Routing weights adjust from outcomes |
| 6 | Evaluate non-code tasks and additional workers | Go/no-go decision |

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
| A2A overhead or immaturity for in-house workers | Slower delivery | Use direct calls where both sides are owned |
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

1. Which harnesses and models are in the first two workers?
2. Does each chosen harness allow redirecting its model endpoint to the gateway?
3. Must workers run on Windows hosts, or Linux only?
4. What regulatory or data-residency constraints apply to code and prompts?
5. What are the budget caps per task and per day?
6. Which repositories and risk tiers are in the pilot?
7. Who owns the scorecard and routing decisions?

## 16. Assumptions

- The first release targets code tasks on a single repository.
- Humans retain merge authority.
- An off-the-shelf gateway and observability backend are acceptable.
- Numeric targets above are placeholders pending Phase 2 baselines.

## 17. Evidence Notes

- Same-model score gaps across harnesses and weaker evidence for open-framework gaps come from benchmark papers and secondary analyses; treat as directional.
- Defect and security statistics for AI-generated code come from a mix of academic and vendor studies.
- A2A and MCP are hosted under the Linux Foundation's Agentic AI Foundation (the exact date and path of A2A's move was reported inconsistently across sources).
- Star counts and download figures vary across sources and include CI and transitive installs; they indicate activity, not user counts.
