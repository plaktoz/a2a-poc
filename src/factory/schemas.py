"""Shapes of the JSON deliverables agents return, shared by agents and the orchestrator.

An agent validates its own reply against the schema its step names (so it can retry its LLM
cheaply), and the orchestrator validates it again as a gate. Each schema has:

- a pydantic model (structure),
- `check(data, ctx)` for rules that need run context, such as the product's surfaces,
- a shape, the example the agent puts in its prompt,
- `render(name, data)`, the text a human reads at an approval checkpoint.

`SCHEMAS[name]` holds the first three in one record.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

APP_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")  # a run's folder name under apps/


class _Open(BaseModel):
    # LLMs add fields; keep them rather than fail, but require the ones we use.
    model_config = ConfigDict(extra="allow")


class Criterion(_Open):
    id: str
    text: str = Field(min_length=1)
    surface: str | None = None


class Spec(_Open):
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    acceptance_criteria: list[Criterion] = Field(min_length=1)
    constraints: list[str] = []
    out_of_scope: list[str] = []
    assumptions: list[str] = []
    questions: list[str] = []


class Endpoint(_Open):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    summary: str = ""
    request: Any = None
    response: Any = None
    status: list[int] = [200]

    @field_validator("method", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v

    @field_validator("path")
    @classmethod
    def _absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"path {v!r} must start with /")
        return v


class ApiContract(_Open):
    endpoints: list[Endpoint] = Field(min_length=1)
    models: dict[str, Any] = {}
    notes: list[str] = []


class SubQuestion(_Open):
    id: str
    question: str = Field(min_length=1)
    why: str = ""


class ResearchPlan(_Open):
    title: str = Field(min_length=1)
    question: str = Field(min_length=1)
    audience: str = ""
    deliverable: str = ""
    sub_questions: list[SubQuestion] = Field(min_length=1, max_length=8)
    out_of_scope: list[str] = []
    assumptions: list[str] = []
    questions: list[str] = []


class Finding(_Open):
    claim: str = Field(min_length=1)
    source_url: str
    source_title: str = ""
    quote: str = Field(min_length=1)
    confidence: Literal["high", "medium", "low"] = "medium"
    verified: bool | None = None


class Findings(_Open):
    sub_question_id: str
    answer: str = Field(min_length=1)
    findings: list[Finding] = []
    gaps: list[str] = []
    rejected: list[dict] = []


class AuditFinding(_Open):
    severity: Literal["critical", "high", "medium", "low"]
    title: str = Field(min_length=1)
    file: str
    line: int = Field(ge=1)
    evidence: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)
    verified: bool | None = None


class AuditFindings(_Open):
    lens: str
    summary: str = Field(min_length=1)
    findings: list[AuditFinding] = []
    rejected: list[dict] = []


class Verification(_Open):
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    verdicts: dict[str, str]


class Verdict(_Open):
    verdict: Literal["approve", "revise"]
    issues: list[str] = []
    strengths: list[str] = []

    @field_validator("verdict", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> Any:
        return v.lower() if isinstance(v, str) else v


class Triage(_Open):
    runbook: str
    traits: dict[str, Any] = {}
    name: str
    rationale: str = Field(min_length=1)
    target: str | None = None


class DevResult(_Open):
    summary: str = ""
    test_change: str = ""


class Report(_Open):
    markdown: str = Field(min_length=1)


def validate(name: str, data: Any, ctx: dict | None = None) -> dict:
    """Return `data` as a plain dict with defaults applied; raise ValueError if unusable."""
    if name not in SCHEMAS:
        raise ValueError(f"unknown schema {name!r}")
    schema = SCHEMAS[name]
    try:
        obj = schema.model.model_validate(data)
    except ValidationError as e:
        errors = "; ".join(f"{'.'.join(map(str, x['loc'])) or '<root>'}: {x['msg']}" for x in e.errors()[:6])
        raise ValueError(f"does not match the {name} shape: {errors}") from None
    out = obj.model_dump(mode="json")
    if schema.check:
        schema.check(out, ctx or {})
    return out


def _check_spec(spec: dict, ctx: dict) -> None:
    criteria = spec["acceptance_criteria"]
    if [c["id"] for c in criteria] != [f"AC{i}" for i in range(1, len(criteria) + 1)]:
        raise ValueError("acceptance criteria ids must be AC1, AC2, ... in order")
    surfaces = list(ctx.get("surfaces") or [])
    if not surfaces:
        return
    if len(surfaces) == 1:
        for c in criteria:
            c["surface"] = c.get("surface") or surfaces[0]
    for c in criteria:
        if c.get("surface") not in surfaces:
            raise ValueError(f"{c['id']} has surface {c.get('surface')!r}; use one of {surfaces}")
    if missing := [s for s in surfaces if not any(c["surface"] == s for c in criteria)]:
        raise ValueError(f"no acceptance criterion for surface {', '.join(missing)}")


def _check_ids(prefix: str, field: str):
    def check(data: dict, ctx: dict) -> None:
        ids = [q["id"] for q in data[field]]
        if ids != [f"{prefix}{i}" for i in range(1, len(ids) + 1)]:
            raise ValueError(f"{field} ids must be {prefix}1, {prefix}2, ... in order")

    return check


def _check_contract(data: dict, ctx: dict) -> None:
    """ctx["api_prefix"], when set, is a prefix every path must start with (a full-stack app's proxy)."""
    prefix = ctx.get("api_prefix")
    seen = set()
    for e in data["endpoints"]:
        if prefix and not e["path"].startswith(prefix):
            raise ValueError(f"path {e['path']!r} must start with {prefix}")
        key = (e["method"], e["path"])
        if key in seen:
            raise ValueError(f"duplicate endpoint {e['method']} {e['path']}")
        seen.add(key)


def _check_triage(data: dict, ctx: dict) -> None:
    """ctx["runbooks"] maps each runbook to its trait names."""
    runbooks = ctx.get("runbooks")
    if not runbooks:
        return
    if data["runbook"] not in runbooks:
        raise ValueError(f"runbook {data['runbook']!r} is not one of {sorted(runbooks)}")
    allowed = set(runbooks[data["runbook"]])
    if unknown := set(data["traits"]) - allowed:
        raise ValueError(f"runbook {data['runbook']} has no trait {', '.join(sorted(unknown))}; "
                         f"its traits are {', '.join(sorted(allowed)) or 'none'}")
    for k, v in data["traits"].items():
        if not isinstance(v, bool):
            raise ValueError(f"trait {k} must be true or false")
    if not APP_NAME.fullmatch(data["name"]):
        raise ValueError("name must be lowercase letters, digits and hyphens, at most 40 characters")


@dataclass(frozen=True)
class Schema:
    """One deliverable: its structure, the example shown to an agent, and its run-context rule."""

    model: type[BaseModel]
    shape: str
    check: Callable[[dict, dict], None] | None = None


SCHEMAS: dict[str, Schema] = {
    "spec": Schema(
        Spec,
        """{"title": str, "goal": str,
 "acceptance_criteria": [{"id": "AC1", "text": str, "surface": str}],
 "constraints": [str], "out_of_scope": [str], "assumptions": [str], "questions": [str]}""", _check_spec),
    "api_contract": Schema(
        ApiContract,
        """{"endpoints": [{"method": "GET|POST|PUT|PATCH|DELETE", "path": "/...",
   "summary": str, "request": <JSON example body or null>, "response": <JSON example body>,
   "status": [int]}],
 "models": {<Name>: <JSON example object>}, "notes": [str]}""", _check_contract),
    "research_plan": Schema(
        ResearchPlan,
        """{"title": str, "question": str, "audience": str, "deliverable": str,
 "sub_questions": [{"id": "Q1", "question": str, "why": str}],
 "out_of_scope": [str], "assumptions": [str], "questions": [str]}""", _check_ids("Q", "sub_questions")),
    "findings": Schema(
        Findings,
        """{"sub_question_id": "Q1", "answer": str,
 "findings": [{"claim": str, "source_url": str, "source_title": str,
               "quote": "<exact sentence copied from the source>", "confidence": "high|medium|low"}],
 "gaps": [str]}"""),
    "audit_findings": Schema(
        AuditFindings,
        """{"lens": str, "summary": str,
 "findings": [{"severity": "critical|high|medium|low", "title": str, "file": "<path relative to
   the project root>", "line": int, "evidence": "<exact code copied from that line or the lines
   around it>", "recommendation": str}]}"""),
    "verification": Schema(
        Verification,
        """{"passed": int, "failed": int,
 "verdicts": {"AC1": "PASS" | "FAIL - <reason>"}}"""),
    "verdict": Schema(
        Verdict,
        """{"verdict": "approve|revise", "issues": [str], "strengths": [str]}"""),
    "triage": Schema(
        Triage,
        """{"runbook": str, "traits": {<trait>: <value>}, "name": "<lowercase-hyphenated
   project name, at most 40 characters>", "rationale": str, "target": "<path or null>"}""", _check_triage),
    "dev_result": Schema(
        DevResult,
        """{"summary": str, "test_change": "<which test you changed and why, or empty>"}"""),
    "report": Schema(
        Report,
        """{"markdown": "<the whole report in Markdown>"}"""),
}


# --- rendering for humans ---------------------------------------------------


def _bullets(title: str, items: list[str]) -> list[str]:
    return ["", f"{title}:", *(f"  - {x}" for x in items)] if items else []


def render(name: str | None, data: Any) -> str:
    if name == "spec":
        lines = [f"# {data['title']}", "", data["goal"], "", "Acceptance criteria:"]
        for c in data["acceptance_criteria"]:
            where = f" [{c['surface']}]" if c.get("surface") else ""
            lines.append(f"  {c['id']}{where}: {c['text']}")
        for key in ("constraints", "out_of_scope", "assumptions"):
            lines += _bullets(key.replace("_", " ").capitalize(), data.get(key, []))
        return "\n".join(lines)
    if name == "research_plan":
        lines = [f"# {data['title']}", "", f"Question: {data['question']}"]
        if data.get("audience"):
            lines.append(f"Audience: {data['audience']}")
        if data.get("deliverable"):
            lines.append(f"Deliverable: {data['deliverable']}")
        lines += ["", "Sub-questions:", *(f"  {q['id']}: {q['question']}" for q in data["sub_questions"])]
        lines += _bullets("Out of scope", data.get("out_of_scope", []))
        lines += _bullets("Assumptions", data.get("assumptions", []))
        return "\n".join(lines)
    if name == "api_contract":
        lines = ["Endpoints:"]
        for e in data["endpoints"]:
            lines.append(f"  {e['method']:<6} {e['path']}  {e.get('summary', '')}")
            for part in ("request", "response"):
                if e.get(part) is not None:
                    lines.append(f"         {part}: {json.dumps(e[part])[:300]}")
        lines += _bullets("Notes", data.get("notes", []))
        return "\n".join(lines)
    if name == "triage":
        traits = ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in data["traits"].items()) or "none"
        return (f"Runbook: {data['runbook']}\nName: {data['name']}\nTraits: {traits}"
                + (f"\nTarget: {data['target']}" if data.get("target") else "")
                + f"\nWhy: {data['rationale']}")
    if name == "audit_findings":
        lines = [f"Lens: {data['lens']}", data["summary"]]
        lines += [f"  [{f['severity']}] {f['file']}:{f['line']} {f['title']}" for f in data["findings"]]
        return "\n".join(lines)
    if name == "report":
        return data["markdown"]
    if name == "findings":
        lines = [f"Answer: {data['answer']}", "", "Verified findings:"]
        for f in data["findings"]:
            lines.append(f"  - {f['claim']} ({f['confidence']} confidence)\n"
                         f"    source: {f['source_url']} {f.get('source_title', '')}\n"
                         f"    quote: \"{f['quote']}\"")
        lines += _bullets("Gaps", data.get("gaps", []))
        return "\n".join(lines)
    if name == "verification":
        return f"passed={data['passed']} failed={data['failed']}\n" + "\n".join(
            f"  {k}: {v}" for k, v in data["verdicts"].items())
    if name == "verdict":
        lines = [f"Verdict: {data['verdict']}"]
        lines += _bullets("Issues", data.get("issues", []))
        lines += _bullets("Strengths", data.get("strengths", []))
        return "\n".join(lines)
    return json.dumps(data, indent=2)[:6000]
