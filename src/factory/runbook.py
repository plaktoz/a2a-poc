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
TEMPLATE_VARS = {"brief", "name", "target", "traits", "surfaces", "repos", "item", "test_files",
                 "source_files", "test_command", "check_commands", "workspace", "inputs",
                 "surface_summaries", "start_commands"}
INTERFACE = "interface"  # a name `reads` accepts: the interface details the user supplied with --interface
INPUT_REF = r"inputs\.([a-z][a-z0-9_-]*)"
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


def input_refs(text: str) -> set[str]:
    """The names of the inputs a text refers to, as {{ inputs.name }}."""
    return {m.split(".")[1] for m in _VAR.findall(text) if m.startswith("inputs.")}


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


class InputDef(_Strict):
    """A value the user supplies for a run (`factory run --input NAME=VALUE`) instead of the runbook fixing it.
    A `text` input is one string, a `number` input is a whole number, and a `list` input is comma-separated
    and can drive `for_each: inputs.<name>`. An input has a default or is required, not both."""
    description: str
    type: Literal["text", "number", "list"] = "text"
    default: str | int | list[str] | None = None
    required: bool = False

    @model_validator(mode="after")
    def _check(self) -> InputDef:
        if self.required and self.default is not None:
            raise ValueError("an input is required or has a default, not both")
        if not self.required and self.default is None:
            raise ValueError("an input needs a default, or required: true")
        if not self.required:
            parse_input(self, self.default)  # the default must fit the type
        return self


def parse_input(definition: InputDef, raw: Any) -> str | int | list[str]:
    """The value of an input as its type holds it: a string, an int, or a list of strings."""
    if definition.type == "number":
        try:
            value = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"{raw!r} is not a whole number") from None
        if value < 1:
            raise ValueError(f"{value} is not at least 1")
        return value
    if definition.type == "list":
        items = raw if isinstance(raw, list) else str(raw).split(",")
        values = [x.strip() for x in map(str, items) if x.strip()]
        if not values:
            raise ValueError("a list input needs at least one value")
        return values
    if isinstance(raw, list):
        raise ValueError("a text input is one value, not a list")
    return str(raw).strip()


class ScaffoldDef(_Strict):
    template: str
    when: str | None = None
    notes: dict[str, str] = {}  # condition -> text added to the stack when it holds


class StaffDef(_Strict):
    worker: str
    model: str | None = None
    url: str | None = None  # an agent that is already running; the factory does not launch it
    fallback: StaffDef | None = None  # used only on a step's last attempt (FR-7)


StaffDef.model_rebuild()


class StepDef(_Strict):
    id: str = Field(pattern=ID)
    role: str
    instructions: str
    when: str | None = None
    min_surfaces: int = Field(default=0, ge=0)  # the step runs only when the app has at least this many surfaces
    for_each: str | list[str] | None = None  # "surfaces", "repos", "<step>.<field>", or a list of names
    parallel: bool = False
    reads: list[str] = []
    output: str | None = None
    gates: list[str] = []
    checkpoint: bool = False
    confirm_input: bool = False  # stop for a person while the output's `questions` are not empty (--yes does not skip it)
    save: str | None = None
    retries: int = Field(default=1, ge=0)
    cwd: Literal["workspace", "surface", "target", "repo"] | None = None  # repo: the item's own repo
    writes: Literal["none", "tests", "source", "target"] = "none"  # target: any file of the repo worktree

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
    rounds: int | str = "budget"  # "budget": 1 + budgets.max_retries; "inputs.<name>": a number input
    when: str | None = None
    steps: list[StepDef] = Field(min_length=1)
    gates: list[str] = []  # run after every step of a round has passed


    @model_validator(mode="after")
    def _rounds(self) -> LoopDef:
        if isinstance(self.rounds, str) and self.rounds != "budget" and not re.fullmatch(INPUT_REF, self.rounds):
            raise ValueError(f"loop {self.loop}: rounds is a number, 'budget' or 'inputs.<name>'")
        return self


Node = StepDef | LoopDef


class Runbook(_Strict):
    name: str = Field(pattern=ID)
    title: str
    description: str
    use_when: str
    needs_target: bool = False
    needs_repos: bool = False  # one or more git repos to read, from --repo (impact-analysis)
    traits: dict[str, TraitDef] = {}
    inputs: dict[str, InputDef] = {}
    scaffolds: dict[str, ScaffoldDef] = {}
    scaffolds_from: str | None = None  # a list input of `directory=template` pairs: the user chooses the surfaces
    staff: dict[str, StaffDef] = {}
    steps: list[Node] = Field(min_length=1)
    finish: list[FinishDef] = []

    @model_validator(mode="after")
    def _check(self) -> Runbook:
        traits = set(self.traits)

        def condition(expr: str | None, where: str) -> None:
            if expr is not None and (unknown := names_in(expr) - traits):
                raise ValueError(f"{where}: unknown trait {', '.join(sorted(unknown))}")

        for n in self.inputs:
            if not re.match(ID, n):
                raise ValueError(f"inputs.{n}: input names are lowercase words")
        declared = set(self.inputs)

        def uses_inputs(text: str, where: str) -> None:
            if bad := input_refs(text) - declared:
                raise ValueError(f"{where}: {{{{ inputs.{sorted(bad)[0]} }}}} is not a declared input")

        if self.scaffolds_from is not None:
            if self.scaffolds:
                raise ValueError("scaffolds_from and scaffolds are two ways to place surfaces; use one")
            if self.scaffolds_from not in self.inputs or self.inputs[self.scaffolds_from].type != "list":
                raise ValueError(f"scaffolds_from {self.scaffolds_from} needs a declared input of type list")
        for d, s in self.scaffolds.items():
            uses_inputs(s.template + "".join(s.notes.values()), f"scaffolds.{d}")
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
            uses_inputs(f.text, "finish")

        seen: set[str] = set()
        for node in self.steps:
            if isinstance(node, LoopDef):
                if (m := re.fullmatch(INPUT_REF, str(node.rounds))) and (
                        m[1] not in self.inputs or self.inputs[m[1]].type != "number"):
                    raise ValueError(f"loop {node.loop}: rounds {node.rounds} needs a declared input of type number")
                condition(node.when, f"loop {node.loop}")
                for g in node.gates:
                    if g not in GATES:
                        raise ValueError(f"loop {node.loop}: unknown gate {g!r}")
                if node.loop in seen:
                    raise ValueError(f"duplicate id {node.loop!r}")
                seen.add(node.loop)
            for step in node.steps if isinstance(node, LoopDef) else [node]:
                uses_inputs(step.instructions + (step.save or ""), f"step {step.id}")
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
        if bad := [r for r in s.reads if r not in seen and r != INTERFACE]:
            raise ValueError(f"{where}: reads {', '.join(bad)}, which no earlier step produces")
        condition(s.when, where)
        if isinstance(s.for_each, str) and s.for_each.startswith("inputs."):
            name = s.for_each.split(".", 1)[1]
            if name not in self.inputs or self.inputs[name].type != "list":
                raise ValueError(f"{where}: for_each {s.for_each} needs a declared input of type list")
        elif isinstance(s.for_each, str) and s.for_each not in ("surfaces", "repos"):
            src = s.for_each.split(".")[0]
            if "." not in s.for_each or src not in seen:
                raise ValueError(f"{where}: for_each must be 'surfaces', 'repos', 'inputs.<list input>', a list, "
                                 "or '<earlier step>.<field>'")
        if (s.for_each == "surfaces" or s.min_surfaces) and not (self.scaffolds or self.scaffolds_from):
            raise ValueError(f"{where}: for_each surfaces and min_surfaces need scaffolds or scaffolds_from")
        if s.for_each == "repos" and not self.needs_repos:
            raise ValueError(f"{where}: for_each repos needs needs_repos: true")
        if s.for_each == "repos" and s.cwd != "repo":
            raise ValueError(f"{where}: for_each repos needs cwd: repo")
        if s.cwd == "repo" and s.for_each != "repos":
            raise ValueError(f"{where}: cwd repo needs for_each: repos")
        if s.cwd == "surface" and s.for_each != "surfaces":
            raise ValueError(f"{where}: cwd surface needs for_each: surfaces")
        if s.cwd == "target" and not self.needs_target:
            raise ValueError(f"{where}: cwd target needs needs_target: true")
        if s.writes == "target" and s.cwd != "target":
            raise ValueError(f"{where}: writes target needs cwd: target")
        if s.writes in ("tests", "source") and s.cwd != "surface":
            raise ValueError(f"{where}: only a step working in a surface may write files")
        if s.confirm_input and (s.output not in ("spec", "research_plan") or s.parallel or s.for_each):
            raise ValueError(f"{where}: confirm_input needs one (not parallel) step that returns a spec or a "
                             "research_plan, which carry `questions`")
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
        if rb.needs_repos:
            out.append("  Needs one or more git repos to read: the paths, given with --repo.")
        for t, d in rb.traits.items():
            out.append(f"  trait {t} (default {str(d.default).lower()}): {d.description}")
        for n, i in rb.inputs.items():
            out.append(f"  input {n} ({'required' if i.required else 'default ' + str(i.default)}): {i.description}")
    return "\n".join(out)


# --- staffing ---------------------------------------------------------------


@dataclass(frozen=True)
class Staff:
    role: str
    worker: str
    model: str
    url: str | None = None
    fallback: Fallback | None = None


@dataclass(frozen=True)
class Fallback:
    """The worker and model that take a role's last attempt when the main one fails (FR-7)."""

    worker: str
    model: str


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
        fb = None
        if d.fallback is not None:
            fb = Fallback(d.fallback.worker, cfg.check_request(d.fallback.worker, d.fallback.model))
            if fb.worker == d.worker and fb.model == cfg.check_request(d.worker, d.model):
                raise ConfigError(f"{path.name}: the fallback of {role} is the same worker and model")
        staff[role] = Staff(role, d.worker, cfg.check_request(d.worker, d.model), d.url, fb)
    if unknown := set(roles) - set(ROLES):
        raise ConfigError(f"{path.name}: unknown roles {', '.join(sorted(unknown))}")
    return staff


# --- plans ------------------------------------------------------------------


def resolve_inputs(rb: Runbook, given: dict[str, str], *, require: bool = True) -> dict[str, Any]:
    """The values of the runbook's inputs: what the user gave, else the default. A required input with no
    value is an error, unless `require` is off (`factory plan` shows the plan without values)."""
    if unknown := set(given) - set(rb.inputs):
        raise ConfigError(f"runbook {rb.name} has no input {', '.join(sorted(unknown))}; "
                          f"its inputs are {', '.join(rb.inputs) or 'none'}")
    values: dict[str, Any] = {}
    for name, d in rb.inputs.items():
        if name in given:
            try:
                values[name] = parse_input(d, given[name])
            except ValueError as e:
                raise ConfigError(f"--input {name}: {e}") from None
        elif d.required:
            if require:
                raise ConfigError(f"runbook {rb.name} needs the input {name} ({d.description}): "
                                  f"pass --input {name}=VALUE")
            values[name] = [f"<{name}>"] if d.type == "list" else f"<{name}>"
        else:
            values[name] = parse_input(d, d.default)
    return values


@dataclass
class Plan:
    runbook: Runbook
    traits: dict[str, bool]
    templates: dict[str, Template]  # surface directory -> template, for the scaffolds that apply
    nodes: list[Node]
    staff: dict[str, Staff]
    notes: dict[str, str] = field(default_factory=dict)  # surface -> extra stack text
    inputs: dict[str, Any] = field(default_factory=dict)  # the user's values for the runbook's inputs
    warnings: list[str] = field(default_factory=list)  # shown before the run, e.g. a one-model checker

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


def parse_surfaces(items: list[str]) -> dict[str, str]:
    """The `directory=template` pairs of a surfaces input, as {directory: template name}."""
    out: dict[str, str] = {}
    for item in items:
        d, sep, t = (x.strip() for x in item.partition("="))
        if not sep or not d or not t:
            raise ConfigError(f"surface {item!r}: use directory=template, such as web=react-vite")
        if not re.fullmatch(ID, d):
            raise ConfigError(f"surface {d!r}: directory names are lowercase words")
        if d in out:
            raise ConfigError(f"surface {d!r} is listed twice")
        out[d] = t
    return out


def resolve(rb: Runbook, traits: dict[str, Any], staff: dict[str, Staff], cfg: Config,
            templates_dir: Path = TEMPLATES_DIR, inputs: dict[str, Any] | None = None) -> Plan:
    values = resolve_inputs(rb, {}, require=False) if inputs is None else inputs
    if unknown := set(traits) - set(rb.traits):
        raise ConfigError(f"runbook {rb.name} has no trait {', '.join(sorted(unknown))}; "
                          f"its traits are {', '.join(rb.traits) or 'none'}")
    final = {t: bool(traits.get(t, d.default)) for t, d in rb.traits.items()}
    templates, notes = {}, {}
    if rb.scaffolds_from:
        for d, name in parse_surfaces(values[rb.scaffolds_from]).items():
            templates[d] = read_template(name, templates_dir)
    for d, s in rb.scaffolds.items():
        if holds(s.when, final):
            templates[d] = read_template(fill(s.template, {"inputs": values}), templates_dir)
            notes[d] = "\n".join(fill(text, {"inputs": values}).strip()
                                 for c, text in s.notes.items() if holds(c, final))
    for d in templates:  # each surface is told what else is in the app, so a template can give rules for both cases
        others = [f"{o} ({t.name}: {t.summary or t.surface})" for o, t in templates.items() if o != d]
        about = ("- The app's other surfaces: " + "; ".join(others) + "." if others
                 else "- This is the only surface of the app: it has no backend or other part to call.")
        notes[d] = "\n".join(x for x in (notes.get(d, ""), about) if x)

    def applies(step: Node) -> bool:
        return holds(step.when, final) and (not isinstance(step, StepDef) or len(templates) >= step.min_surfaces)

    nodes: list[Node] = []
    for n in rb.steps:
        if not applies(n):
            continue
        if isinstance(n, LoopDef):
            inner = [s for s in n.steps if applies(s)]
            if inner:
                rounds = values[m[1]] if (m := re.fullmatch(INPUT_REF, str(n.rounds))) else n.rounds
                nodes.append(n.model_copy(update={"steps": inner, "rounds": rounds}))
        else:
            nodes.append(n)
    needs_surfaces = any(s.for_each == "surfaces" for n in nodes
                         for s in (n.steps if isinstance(n, LoopDef) else [n]))
    if needs_surfaces and not templates:
        raise ConfigError(f"runbook {rb.name}: no scaffold applies, so there is nothing to build. "
                          "Turn on a surface trait or give the surfaces input.")
    plan = Plan(rb, final, templates, nodes, {**staff}, notes, values)
    _check_independence(plan, cfg)
    return plan


def _check_independence(plan: Plan, cfg: Config) -> None:
    """Independence: a checking role must not use the model of the role whose work it checks.

    When the config offers only one model, no pair can differ. The plan then runs and carries a
    warning (`plan.warnings`), because a single-model setup is allowed. The rule still refuses a
    shared model whenever another model is enabled for the roles to use."""
    if not cfg.reviewer.must_differ_from_author:
        return
    used = plan.roles()
    available = {m for w in cfg.enabled_workers() for m in w.models}
    for checker, author in (("reviewer", "writer"), ("reviewer", "architect"), ("reviewer", "developer"),
                            ("verifier", "developer")):
        if checker not in used or author not in used:
            continue
        # A fallback must keep independence too, so a switch cannot break it (FR-7).
        checker_models = {m for m in _models(plan.staff[checker])}
        author_models = {m for m in _models(plan.staff[author])}
        if checker_models & author_models:
            shared = sorted(checker_models & author_models)[0]
            if len(available) < 2:
                plan.warnings.append(
                    f"the {checker} and the {author} both use {shared}. Only one model is enabled, so "
                    "the checker rule cannot be met. The best practice is a different model for the "
                    f"{checker}: enable a second worker in llm.yaml and set it in agents.yaml.")
                continue
            raise ConfigError(
                f"the {checker} and the {author} both use {shared}; the {checker} "
                "must use a different model (reviewer.must_differ_from_author). Change agents.yaml."
            )


def _models(st: Staff) -> set[str]:
    """The models a role may use: its main model and its fallback's, if any."""
    return {st.model} | ({st.fallback.model} if st.fallback else set())


def outline(plan: Plan) -> str:
    """The plan as text, for a person to check before anything runs."""
    rb = plan.runbook
    lines = [f"Runbook {rb.name}: {rb.title}"]
    if plan.traits:
        lines.append("Traits: " + ", ".join(f"{t}={'yes' if v else 'no'}" for t, v in plan.traits.items()))
    if plan.templates:
        lines.append("Scaffolds: " + ", ".join(f"{d}/ ({t.name})" for d, t in plan.templates.items()))
    for name, value in plan.inputs.items():
        shown = ", ".join(map(str, value)) if isinstance(value, list) else " ".join(str(value).split())
        shown = shown if len(shown) <= 100 else shown[:97] + "..."
        lines.append(f"Input {name}: {shown}")
    lines += [f"Warning: {w}" for w in plan.warnings]
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
    if for_each == "repos":
        return "repo"
    if for_each.startswith("inputs."):
        return "of " + ", ".join(map(str, plan.inputs.get(for_each[7:], [])))
    step, _, field = for_each.partition(".")
    return f"item of {step}.{field}"
