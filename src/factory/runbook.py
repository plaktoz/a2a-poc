"""Runbooks: what kind of work a request is, as data.

A runbook is a YAML file in runbooks/. It declares the traits a request of its kind can have
(frontend? backend? write code at all?), the scaffolds those traits switch on, and an ordered
list of steps. Each step names the agent role that does it, the instructions, the shape of the
JSON it returns, the gates its output must pass, and whether a person approves it. Steps can
repeat for each item of a list (each surface, each research sub-question, each audit lens), and
a `loop` repeats a group of steps until its gates pass or its rounds run out.

`resolve(runbook, traits)` turns a runbook and a request's traits into a Plan: the steps that
apply, the scaffolds to place, and the worker and model staffing each role.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import schemas
from .config import Config, ConfigError
from .gates import GATES
from .paths import RUNBOOKS_DIR, TEMPLATES_DIR
from .roles import ROLES
from .scaffold import Template, read_template

ID = r"^[a-z][a-z0-9_-]*$"
TEMPLATE_VARS = {"brief", "name", "target", "traits", "surfaces", "item", "test_files",
                 "source_files", "test_command", "check_commands", "workspace"}
_VAR = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- trait expressions ------------------------------------------------------


def _expr(expr: str) -> ast.Expression:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        raise ValueError(f"cannot parse condition {expr!r}") from None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
                                 ast.Name, ast.Load)):
            raise ValueError(f"condition {expr!r}: use trait names with and, or, not and parentheses")
    return tree


def names_in(expr: str) -> set[str]:
    return {n.id for n in ast.walk(_expr(expr)) if isinstance(n, ast.Name)}


def holds(expr: str | None, traits: dict[str, bool]) -> bool:
    """Whether a condition such as "frontend and not backend" holds for these traits."""
    if expr is None:
        return True

    def ev(n: ast.AST) -> bool:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.BoolOp):
            values = [ev(v) for v in n.values]
            return all(values) if isinstance(n.op, ast.And) else any(values)
        if isinstance(n, ast.UnaryOp):
            return not ev(n.operand)
        assert isinstance(n, ast.Name)
        return bool(traits[n.id])

    return ev(_expr(expr))


# --- instruction templates --------------------------------------------------


def template_vars(text: str) -> set[str]:
    return {m.split(".")[0] for m in _VAR.findall(text)}


def fill(text: str, values: dict[str, Any]) -> str:
    """Replace each {{ name.path }} with its value. Lists are joined with commas."""

    def sub(m: re.Match[str]) -> str:
        value: Any = values
        for part in m.group(1).split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise ConfigError(f"{{{{ {m.group(1)} }}}} has no value here")
        if isinstance(value, (list, tuple)):
            return ", ".join(map(str, value))
        if isinstance(value, dict):
            return json.dumps(value, indent=2)
        return "" if value is None else str(value)

    return _VAR.sub(sub, text)


# --- definitions ------------------------------------------------------------


class TraitDef(_Strict):
    description: str
    default: bool = False


class ScaffoldDef(_Strict):
    template: str
    when: str | None = None
    notes: dict[str, str] = {}  # condition -> text added to the stack when it holds


class StaffDef(_Strict):
    worker: str
    model: str | None = None
    url: str | None = None  # an agent that is already running; the factory does not launch it


class StepDef(_Strict):
    id: str = Field(pattern=ID)
    role: str
    instructions: str
    when: str | None = None
    for_each: str | list[str] | None = None  # "surfaces", "<step>.<field>", or a list of names
    parallel: bool = False
    reads: list[str] = []
    output: str | None = None
    gates: list[str] = []
    checkpoint: bool = False
    save: str | None = None
    retries: int = Field(default=1, ge=0)
    cwd: Literal["workspace", "surface", "target"] | None = None
    writes: Literal["none", "tests", "source"] = "none"

    @model_validator(mode="after")
    def _defaults(self) -> StepDef:
        if self.cwd is None:
            self.cwd = "surface" if self.for_each == "surfaces" else "workspace"
        return self


class FinishDef(_Strict):
    text: str
    when: str | None = None


class LoopDef(_Strict):
    loop: str = Field(pattern=ID)
    rounds: int | Literal["budget"] = "budget"  # "budget": 1 + budgets.max_retries
    when: str | None = None
    steps: list[StepDef] = Field(min_length=1)
    gates: list[str] = []  # run after every step of a round has passed


Node = StepDef | LoopDef


class Runbook(_Strict):
    name: str = Field(pattern=ID)
    title: str
    description: str
    use_when: str
    needs_target: bool = False
    traits: dict[str, TraitDef] = {}
    scaffolds: dict[str, ScaffoldDef] = {}
    staff: dict[str, StaffDef] = {}
    steps: list[Node] = Field(min_length=1)
    finish: list[FinishDef] = []

    @model_validator(mode="after")
    def _check(self) -> Runbook:
        traits = set(self.traits)

        def condition(expr: str | None, where: str) -> None:
            if expr is not None and (unknown := names_in(expr) - traits):
                raise ValueError(f"{where}: unknown trait {', '.join(sorted(unknown))}")

        for d, s in self.scaffolds.items():
            if not re.match(ID, d):
                raise ValueError(f"scaffolds.{d}: directory names are lowercase words")
            condition(s.when, f"scaffolds.{d}.when")
            for c in s.notes:
                condition(c, f"scaffolds.{d}.notes")
        for role in self.staff:
            if role not in ROLES:
                raise ValueError(f"staff.{role}: unknown role")
        for f in self.finish:
            condition(f.when, "finish.when")

        seen: set[str] = set()
        for node in self.steps:
            if isinstance(node, LoopDef):
                condition(node.when, f"loop {node.loop}")
                for g in node.gates:
                    if g not in GATES:
                        raise ValueError(f"loop {node.loop}: unknown gate {g!r}")
                if node.loop in seen:
                    raise ValueError(f"duplicate id {node.loop!r}")
                seen.add(node.loop)
            for step in node.steps if isinstance(node, LoopDef) else [node]:
                self._check_step(step, seen, condition)
                seen.add(step.id)
        return self

    def _check_step(self, s: StepDef, seen: set[str], condition) -> None:
        where = f"step {s.id}"
        if s.id in seen:
            raise ValueError(f"duplicate id {s.id!r}")
        if s.role not in ROLES:
            raise ValueError(f"{where}: unknown role {s.role!r}; roles are {', '.join(ROLES)}")
        if s.output is not None and s.output not in schemas.SCHEMAS:
            raise ValueError(f"{where}: unknown output {s.output!r}; outputs are {', '.join(schemas.SCHEMAS)}")
        if bad := [g for g in s.gates if g not in GATES]:
            raise ValueError(f"{where}: unknown gate {', '.join(bad)}; gates are {', '.join(GATES)}")
        if bad := [r for r in s.reads if r not in seen]:
            raise ValueError(f"{where}: reads {', '.join(bad)}, which no earlier step produces")
        condition(s.when, where)
        if isinstance(s.for_each, str) and s.for_each != "surfaces":
            src = s.for_each.split(".")[0]
            if "." not in s.for_each or src not in seen:
                raise ValueError(f"{where}: for_each must be 'surfaces', a list, or '<earlier step>.<field>'")
        if s.for_each == "surfaces" and not self.scaffolds:
            raise ValueError(f"{where}: for_each surfaces, but the runbook has no scaffolds")
        if s.cwd == "surface" and s.for_each != "surfaces":
            raise ValueError(f"{where}: cwd surface needs for_each: surfaces")
        if s.cwd == "target" and not self.needs_target:
            raise ValueError(f"{where}: cwd target needs needs_target: true")
        if s.writes != "none" and s.cwd != "surface":
            raise ValueError(f"{where}: only a step working in a surface may write files")
        if s.checkpoint and s.parallel:
            raise ValueError(f"{where}: a checkpoint step cannot run in parallel")
        if s.writes != "none" and not ROLES[s.role].edit:
            raise ValueError(f"{where}: role {s.role} cannot edit files")
        if bad := template_vars(s.instructions + (s.save or "")) - TEMPLATE_VARS:
            raise ValueError(f"{where}: unknown template variable {', '.join(sorted(bad))}")


def load_runbook(path: Path) -> Runbook:
    try:
        return Runbook.model_validate(yaml.safe_load(path.read_text()))
    except ValidationError as e:
        errors = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:5])
        raise ConfigError(f"{path.name}: {errors}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name}: invalid YAML: {e}") from None


def catalog(directory: Path = RUNBOOKS_DIR) -> dict[str, Runbook]:
    books = {}
    for p in sorted(directory.glob("*.yaml")):
        rb = load_runbook(p)
        if rb.name != p.stem:
            raise ConfigError(f"{p.name}: name {rb.name!r} must match the file name")
        books[rb.name] = rb
    return books


def describe_catalog(books: dict[str, Runbook]) -> str:
    """The runbook list a triage agent chooses from."""
    out = []
    for rb in books.values():
        out.append(f"- {rb.name}: {rb.title}. {rb.description.strip()}\n  Use when: {rb.use_when.strip()}")
        if rb.needs_target:
            out.append("  Needs a target: the path of existing code to work on.")
        for t, d in rb.traits.items():
            out.append(f"  trait {t} (default {str(d.default).lower()}): {d.description}")
    return "\n".join(out)


# --- staffing ---------------------------------------------------------------


@dataclass(frozen=True)
class Staff:
    role: str
    worker: str
    model: str
    url: str | None = None


def load_staff(path: Path, cfg: Config, overrides: dict[str, StaffDef] | None = None) -> dict[str, Staff]:
    """Which worker and model play each role: agents.yaml, then a runbook's own `staff`."""
    raw = yaml.safe_load(path.read_text()) or {}
    try:
        roles = {r: StaffDef.model_validate(v) for r, v in (raw.get("roles") or {}).items()}
    except ValidationError as e:
        raise ConfigError(f"{path.name}: {e.errors()[0]['msg']}") from None
    roles.update(overrides or {})
    staff = {}
    for role in ROLES:
        if role not in roles:
            raise ConfigError(f"{path.name}: no worker for role {role!r}")
        d = roles[role]
        staff[role] = Staff(role, d.worker, cfg.check_request(d.worker, d.model), d.url)
    if unknown := set(roles) - set(ROLES):
        raise ConfigError(f"{path.name}: unknown roles {', '.join(sorted(unknown))}")
    return staff


# --- plans ------------------------------------------------------------------


@dataclass
class Plan:
    runbook: Runbook
    traits: dict[str, bool]
    templates: dict[str, Template]  # surface directory -> template, for the scaffolds that apply
    nodes: list[Node]
    staff: dict[str, Staff]
    notes: dict[str, str] = field(default_factory=dict)  # surface -> extra stack text

    @property
    def surfaces(self) -> list[str]:
        return list(self.templates)

    def steps(self) -> list[StepDef]:
        return [s for n in self.nodes for s in (n.steps if isinstance(n, LoopDef) else [n])]

    def roles(self) -> list[str]:
        used = {s.role for s in self.steps()}
        if any(s.writes == "source" for s in self.steps()):
            used.add("tester")  # reviews any change the developer makes to the tests
        return [r for r in ROLES if r in used]


def resolve(rb: Runbook, traits: dict[str, Any], staff: dict[str, Staff], cfg: Config,
            templates_dir: Path = TEMPLATES_DIR) -> Plan:
    if unknown := set(traits) - set(rb.traits):
        raise ConfigError(f"runbook {rb.name} has no trait {', '.join(sorted(unknown))}; "
                          f"its traits are {', '.join(rb.traits) or 'none'}")
    final = {t: bool(traits.get(t, d.default)) for t, d in rb.traits.items()}
    templates, notes = {}, {}
    for d, s in rb.scaffolds.items():
        if holds(s.when, final):
            templates[d] = read_template(s.template, templates_dir)
            notes[d] = "\n".join(text.strip() for c, text in s.notes.items() if holds(c, final))
    nodes: list[Node] = []
    for n in rb.steps:
        if not holds(n.when, final):
            continue
        if isinstance(n, LoopDef):
            inner = [s for s in n.steps if holds(s.when, final)]
            if inner:
                nodes.append(n.model_copy(update={"steps": inner}))
        else:
            nodes.append(n)
    needs_surfaces = any(s.for_each == "surfaces" for n in nodes
                         for s in (n.steps if isinstance(n, LoopDef) else [n]))
    if needs_surfaces and not templates:
        raise ConfigError(f"runbook {rb.name}: these traits switch on no scaffold, so there is "
                          "nothing to build. Turn on at least one surface trait.")
    plan = Plan(rb, final, templates, nodes, {**staff}, notes)
    _check_independence(plan, cfg)
    return plan


def _check_independence(plan: Plan, cfg: Config) -> None:
    """Independence: a checking role must not use the model of the role whose work it checks."""
    if not cfg.reviewer.must_differ_from_author:
        return
    used = plan.roles()
    for checker, author in (("reviewer", "writer"), ("reviewer", "architect"), ("verifier", "developer")):
        if checker in used and author in used and plan.staff[checker].model == plan.staff[author].model:
            raise ConfigError(
                f"the {checker} and the {author} both use {plan.staff[author].model}; the {checker} "
                "must use a different model (reviewer.must_differ_from_author). Change agents.yaml."
            )


def outline(plan: Plan) -> str:
    """The plan as text, for a person to check before anything runs."""
    rb = plan.runbook
    lines = [f"Runbook {rb.name}: {rb.title}"]
    if plan.traits:
        lines.append("Traits: " + ", ".join(f"{t}={'yes' if v else 'no'}" for t, v in plan.traits.items()))
    if plan.templates:
        lines.append("Scaffolds: " + ", ".join(f"{d}/ ({t.name})" for d, t in plan.templates.items()))
    lines.append("")
    n = 0

    def step_line(s: StepDef, indent: str) -> str:
        nonlocal n
        n += 1
        st = plan.staff[s.role]
        each = (f" for each {_each(s.for_each, plan)}"
                f"{' (in parallel)' if s.parallel else ''}" if s.for_each else "")
        extra = [f"returns {s.output}"] if s.output else []
        extra += [f"gates: {', '.join(s.gates)}"] if s.gates else []
        extra += ["you approve it"] if s.checkpoint else []
        extra += [f"saves {s.save}"] if s.save else []
        return (f"{indent}{n}. {s.id}{each}: {ROLES[s.role].title} ({st.worker} / {st.model})"
                + (f"\n{indent}   {'; '.join(extra)}" if extra else ""))

    for node in plan.nodes:
        if isinstance(node, LoopDef):
            rounds = node.rounds if isinstance(node.rounds, int) else "1 + max_retries"
            lines.append(f"   loop {node.loop}, up to {rounds} rounds"
                         + (f"; then gates: {', '.join(node.gates)}" if node.gates else ""))
            lines += [step_line(s, "     ") for s in node.steps]
        else:
            lines.append(step_line(node, "   "))
    return "\n".join(lines)


def mermaid(plan: Plan) -> str:
    """The plan as a Mermaid flowchart."""
    out, prev = ["flowchart TD", '  start(["request"])'], "start"

    def box(s: StepDef) -> str:
        st = plan.staff[s.role]
        label = f"{s.id}<br/>{ROLES[s.role].title}<br/><i>{st.worker} / {st.model}</i>"
        if s.for_each:
            label += f"<br/>for each {_each(s.for_each, plan)}"
        shape = ('{{"', '"}}') if s.checkpoint else ('["', '"]')
        return f"  {s.id}{shape[0]}{label}{shape[1]}"

    for node in plan.nodes:
        if isinstance(node, LoopDef):
            out.append(f'  subgraph {node.loop}["loop {node.loop}"]')
            out += [box(s) for s in node.steps]
            out += [f"  {a.id} --> {b.id}" for a, b in zip(node.steps, node.steps[1:])]
            out.append("  end")
            out.append(f"  {prev} --> {node.steps[0].id}")
            out.append(f"  {node.steps[-1].id} -. next round .-> {node.steps[0].id}")
            prev = node.steps[-1].id
        else:
            out.append(box(node))
            out.append(f"  {prev} --> {node.id}")
            prev = node.id
    out += ['  done(["deliverable"])', f"  {prev} --> done"]
    return "\n".join(out)


def _each(for_each: str | list, plan: Plan) -> str:
    """How a step's for_each reads in a plan: 'surface (web, api)', 'of security, tests', ..."""
    if isinstance(for_each, list):
        return "of " + ", ".join(map(str, for_each))
    if for_each == "surfaces":
        return f"surface ({', '.join(plan.surfaces)})"
    step, _, field = for_each.partition(".")
    return f"item of {step}.{field}"
