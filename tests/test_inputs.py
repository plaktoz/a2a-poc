"""Runbook inputs: values come from the user, not from the runbook. The human gate (`confirm_input`) stops a
run while the spec has open questions. Interface details arrive with --interface and never live in a runbook.
No LLM."""

import json
import re
from pathlib import Path

import pytest
import yaml

from factory import ConfigError, cli
from factory.paths import RUNBOOKS_DIR
from factory.runbook import InputDef, Runbook, catalog, input_refs, load_runbook, resolve, resolve_inputs
from test_approve import answer, record_of, resumed
from test_engine import SPEC, Agents, make_engine
from test_usage import cfg, staff  # noqa: F401  (fixtures)


def runbook(**over):
    """A small valid runbook, with `over` replacing its top-level keys."""
    base = {"name": "demo", "title": "Demo", "description": "d", "use_when": "u",
            "inputs": {"limit": {"description": "most findings", "type": "number", "default": 5}},
            "steps": [{"id": "plan", "role": "analyst", "instructions": "Report at most {{ inputs.limit }}.",
                       "output": "research_plan"}]}
    return Runbook.model_validate({**base, **over})


# --- the model ----------------------------------------------------------------


def test_an_input_has_a_default_or_is_required_not_both_and_not_neither():
    with pytest.raises(ValueError, match="not both"):
        InputDef(description="d", default="x", required=True)
    with pytest.raises(ValueError, match="needs a default"):
        InputDef(description="d")
    assert InputDef(description="d", required=True).required


def test_a_default_must_fit_the_type():
    with pytest.raises(ValueError, match="not a whole number"):
        InputDef(description="d", type="number", default="many")
    with pytest.raises(ValueError, match="at least one"):
        InputDef(description="d", type="list", default=[])


def test_an_instruction_may_use_only_declared_inputs():
    with pytest.raises(ValueError, match="not a declared input"):
        runbook(steps=[{"id": "plan", "role": "analyst", "instructions": "{{ inputs.nope }}"}])


def test_for_each_and_rounds_need_the_right_kind_of_input():
    step = {"id": "a", "role": "auditor", "for_each": "inputs.limit", "instructions": "x"}
    with pytest.raises(ValueError, match="type list"):
        runbook(steps=[step])
    loop = {"loop": "w", "rounds": "inputs.limit", "steps": [{"id": "a", "role": "writer", "instructions": "x"}]}
    assert runbook(steps=[loop]).steps[0].rounds == "inputs.limit"
    with pytest.raises(ValueError, match="type number"):
        runbook(inputs={"limit": {"description": "d", "default": "5"}}, steps=[loop])
    with pytest.raises(ValueError, match="rounds is a number"):
        runbook(steps=[{**loop, "rounds": "whenever"}])


def test_confirm_input_needs_one_step_that_returns_questions():
    with pytest.raises(ValueError, match="confirm_input"):
        runbook(steps=[{"id": "t", "role": "tester", "instructions": "x", "confirm_input": True}])


def test_input_refs_finds_the_names():
    assert input_refs("a {{ inputs.one }} b {{ brief }} {{inputs.two}}") == {"one", "two"}


# --- values -------------------------------------------------------------------


def test_the_user_value_wins_over_the_default_and_is_typed():
    rb = runbook(inputs={"limit": {"description": "d", "type": "number", "default": 5},
                         "lenses": {"description": "d", "type": "list", "default": ["a"]},
                         "note": {"description": "d", "default": "x"}})
    assert resolve_inputs(rb, {}) == {"limit": 5, "lenses": ["a"], "note": "x"}
    assert resolve_inputs(rb, {"limit": "9", "lenses": "a, b ,c", "note": " hi "}) == {
        "limit": 9, "lenses": ["a", "b", "c"], "note": "hi"}


def test_a_required_input_must_be_given_but_a_plan_may_leave_it_out():
    rb = runbook(inputs={"stack": {"description": "the stack", "required": True}},
                 steps=[{"id": "plan", "role": "analyst", "instructions": "Use {{ inputs.stack }}."}])
    with pytest.raises(ConfigError, match="needs the input stack"):
        resolve_inputs(rb, {})
    assert resolve_inputs(rb, {}, require=False) == {"stack": "<stack>"}


def test_an_unknown_or_bad_input_is_refused_with_its_name():
    rb = runbook()
    with pytest.raises(ConfigError, match="no input colour; its inputs are limit"):
        resolve_inputs(rb, {"colour": "red"})
    with pytest.raises(ConfigError, match="--input limit: 'lots' is not a whole number"):
        resolve_inputs(rb, {"limit": "lots"})


def test_the_cli_reads_inputs_and_the_interface_file(tmp_path):
    assert cli.parse_inputs(["a=1", "b = x=y"]) == {"a": "1", "b": "x=y"}
    for bad in ("a", "=1", "a="):
        with pytest.raises(cli.UsageError):
            cli.parse_inputs([bad])
    assert cli.read_interface(None) == ""
    f = tmp_path / "iface.md"
    f.write_text("GET /v1/items")
    assert cli.read_interface(f) == "GET /v1/items"
    f.write_text("  \n")
    with pytest.raises(cli.UsageError, match="empty"):
        cli.read_interface(f)
    with pytest.raises(cli.UsageError, match="cannot read"):
        cli.read_interface(tmp_path / "missing.md")


# --- the plan -----------------------------------------------------------------


def test_inputs_choose_the_template_the_lenses_and_the_rounds(cfg, staff):
    web = resolve(catalog()["app-build"], {}, staff, cfg,
                  inputs=resolve_inputs(catalog()["app-build"], {"surfaces": "web=react-vite,api=fastapi"}))
    assert web.templates["api"].name == "fastapi"
    audit = catalog()["code-audit"]
    plan = resolve(audit, {}, staff, cfg, inputs=resolve_inputs(audit, {"lenses": "security,perf"}))
    assert [i.key for i in _items(plan, "audit")] == ["security", "perf"]
    research = catalog()["research"]
    plan = resolve(research, {}, staff, cfg, inputs=resolve_inputs(research, {"write_rounds": "5"}))
    assert next(n for n in plan.nodes if getattr(n, "loop", None) == "write").rounds == 5


def test_the_plan_prints_the_input_values(cfg, staff):
    from factory.runbook import outline
    audit = catalog()["code-audit"]
    text = outline(resolve(audit, {}, staff, cfg, inputs=resolve_inputs(audit, {"max_findings": "7", "lenses": "a,b"})))
    assert "Input max_findings: 7" in text and "Input lenses: a, b" in text


def _items(plan, step_id):
    from factory.engine import Engine
    engine = Engine.__new__(Engine)
    engine.plan, engine.repos, engine.artifacts = plan, [], {}
    return engine._items(next(s for s in plan.steps() if s.id == step_id))


def test_a_template_that_does_not_exist_is_an_error_naming_it(cfg, staff):
    web = catalog()["app-build"]
    with pytest.raises(ConfigError, match="template 'nope'"):
        resolve(web, {}, staff, cfg, inputs=resolve_inputs(web, {"surfaces": "web=nope"}))


# --- surfaces: the user chooses the parts of the app ------------------------------


def test_a_surface_is_told_what_else_is_in_the_app(cfg, staff):
    rb = catalog()["app-build"]
    one = resolve(rb, {}, staff, cfg, inputs=resolve_inputs(rb, {"surfaces": "web=react-vite"}))
    two = resolve(rb, {}, staff, cfg, inputs=resolve_inputs(rb, {"surfaces": "web=react-vite,api=fastapi"}))
    assert "only surface" in one.notes["web"]
    assert "api (fastapi" in two.notes["web"] and "web (react-vite" in two.notes["api"]


def test_the_contract_step_needs_two_surfaces(cfg, staff):
    rb = catalog()["app-build"]
    for given, steps in (("web=react-vite", ["spec", "tests", "develop", "verify"]),
                         ("api=fastapi", ["spec", "tests", "develop", "verify"]),
                         ("web=react-vite,api=fastapi", ["spec", "contract", "tests", "develop", "verify"])):
        plan = resolve(rb, {}, staff, cfg, inputs=resolve_inputs(rb, {"surfaces": given}))
        assert [x.id for x in plan.steps()] == steps


def test_scaffolds_from_needs_a_list_input_and_excludes_scaffolds():
    with pytest.raises(ValueError, match="needs a declared input of type list"):
        runbook(scaffolds_from="limit")
    with pytest.raises(ValueError, match="use one"):
        runbook(inputs={"parts": {"description": "d", "type": "list", "default": ["a=b"]}}, scaffolds_from="parts",
                scaffolds={"web": {"template": "react-vite"}})


def test_triage_sets_inputs_and_they_are_checked_against_their_type():
    from factory.schemas import validate
    books = catalog()
    ctx = {"runbooks": {n: list(rb.traits) for n, rb in books.items()},
           "inputs": {n: rb.inputs for n, rb in books.items()}}
    ok = {"runbook": "app-build", "name": "family-list", "rationale": "r",
          "inputs": {"surfaces": "web=react-vite, api=fastapi"}}
    assert validate("triage", ok, ctx)["inputs"] == {"surfaces": ["web=react-vite", "api=fastapi"]}
    with pytest.raises(ValueError, match="no input colour"):
        validate("triage", {**ok, "inputs": {"colour": "red"}}, ctx)
    with pytest.raises(ValueError, match="input criteria_count"):
        validate("triage", {**ok, "inputs": {"criteria_count": ["a"]}}, ctx)


def test_triage_inputs_are_shown_and_a_command_line_input_wins():
    from factory.schemas import render, validate
    out = validate("triage", {"runbook": "app-build", "name": "x", "rationale": "r",
                              "inputs": {"surfaces": ["web=react-vite", "api=fastapi"]}})
    assert "Inputs: surfaces=web=react-vite, api=fastapi" in render("triage", out)


# --- the human gate ------------------------------------------------------------

QUESTION_SPEC = {**SPEC, "questions": ["Which port does the API use?"]}


def web_agents(spec):
    return Agents({("analyst", "spec"): [spec]})


def web_plan(cfg, staff):
    return resolve(catalog()["app-build"], {}, staff, cfg)


def test_open_questions_stop_the_run_even_with_yes(cfg, staff, tmp_path):
    engine = make_engine(web_plan(cfg, staff), cfg, tmp_path, web_agents(QUESTION_SPEC))  # no wait: like --yes
    assert not engine.run()
    rec = record_of(engine.ws)
    assert rec["status"] == "awaiting" and rec["awaiting"]["point"] == "spec:input"
    assert not rec.get("approvals")  # --yes did not answer it
    assert (engine.ws / "SPEC.json").exists()  # the draft is saved for the person to read
    assert not any(role == "tester" for role, _ in engine.send.requests)  # nothing was built


def test_no_open_questions_means_no_stop(cfg, staff, tmp_path):
    spec = {**SPEC, "questions": []}
    engine = make_engine(web_plan(cfg, staff), cfg, tmp_path, web_agents(spec), wait_at_checkpoints=True)
    assert not engine.run()
    rec = record_of(engine.ws)
    assert rec["awaiting"]["point"] == "spec"  # straight to the ordinary spec checkpoint
    assert all(a["point"] != "spec:input" for a in rec.get("approvals", []))


def test_a_person_confirms_the_spec_and_the_run_goes_on_to_the_checkpoint(cfg, staff, tmp_path):
    plan = web_plan(cfg, staff)
    engine = make_engine(plan, cfg, tmp_path, web_agents(QUESTION_SPEC), wait_at_checkpoints=True)
    assert not engine.run()
    answer(engine.ws)  # accept spec:input
    again = Agents({})
    assert not resumed(plan, cfg, engine.ws, again).run()
    rec = record_of(engine.ws)
    assert rec["awaiting"]["point"] == "spec" and not again.requests  # the spec checkpoint is next, spec kept


def test_answers_send_the_spec_back_to_the_analyst(cfg, staff, tmp_path):
    plan = web_plan(cfg, staff)
    engine = make_engine(plan, cfg, tmp_path, web_agents(QUESTION_SPEC), wait_at_checkpoints=True)
    assert not engine.run()
    answer(engine.ws, "changes", "the API uses port 9000")
    agents = web_agents({**SPEC, "questions": []})
    assert not resumed(plan, cfg, engine.ws, agents).run()
    req = next(r for role, r in agents.requests if role == "analyst")
    assert "the API uses port 9000" in req["feedback"] and "Your previous draft" in req["feedback"]


# --- the interface ---------------------------------------------------------------


def test_interface_details_reach_the_agents_as_context_and_are_kept_for_a_resume(cfg, staff, tmp_path):
    plan = web_plan(cfg, staff)
    agents = web_agents({**SPEC, "questions": ["q"]})
    engine = make_engine(plan, cfg, tmp_path, agents, wait_at_checkpoints=True, interface="GET /v1/items -> 200")
    engine.run()
    req = next(r for role, r in agents.requests if role == "analyst")
    titles = [c["title"] for c in req["context"]]
    assert "Interface details (supplied by the user)" in titles
    assert "GET /v1/items" in next(c["body"] for c in req["context"] if c["title"].startswith("Interface"))
    assert record_of(engine.ws)["interface"] == "GET /v1/items -> 200"


def test_without_an_interface_the_steps_get_no_interface_context(cfg, staff, tmp_path):
    agents = web_agents({**SPEC, "questions": ["q"]})
    make_engine(web_plan(cfg, staff), cfg, tmp_path, agents, wait_at_checkpoints=True).run()
    req = next(r for role, r in agents.requests if role == "analyst")
    assert not any(c["title"].startswith("Interface") for c in req["context"])


def test_the_run_record_keeps_the_inputs(cfg, staff, tmp_path):
    web = catalog()["app-build"]
    plan = resolve(web, {}, staff, cfg, inputs=resolve_inputs(web, {"criteria_count": "3 to 5"}))
    engine = make_engine(plan, cfg, tmp_path, web_agents({**SPEC, "questions": ["q"]}), wait_at_checkpoints=True)
    engine.run()
    assert record_of(engine.ws)["inputs"]["criteria_count"] == "3 to 5"
    spec_request = next(r for role, r in engine.send.requests if role == "analyst")
    assert "3 to 5" in spec_request["instructions"]


# --- the shipped runbooks hold no hardcoded user values or interface details ----------------


def shipped():
    return [(p.stem, p.read_text()) for p in sorted(Path(RUNBOOKS_DIR).glob("*.yaml"))]


def body(text: str) -> str:
    """A runbook without its comment lines."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


@pytest.mark.parametrize("name,text", shipped())
def test_no_runbook_holds_interface_details(name, text):
    code = body(text)
    for detail in (r"/api/", r"fetch\(", r"vi\.spyOn", r"uvicorn", r"npm run", r"127\.0\.0\.1", r"localhost",
                   r"\bport \d+", r"App\.jsx", r"app/routers", r"create_app", r"\.\./api"):
        assert not re.search(detail, code), f"{name}: holds {detail}"


@pytest.mark.parametrize("name,text", shipped())
def test_no_runbook_names_a_stack_or_a_fixed_count(name, text):
    code = body(text)
    for stack in ("React", "FastAPI", "pytest", "Vite", "pydantic"):
        assert stack not in yaml.safe_dump(yaml.safe_load(code)).replace("react-vite", ""), f"{name}: names {stack}"
    steps = yaml.safe_load(code)["steps"]
    text_of_steps = json.dumps(steps)
    if name == "app-build":  # the steps of the general builder name no kind of surface; the description may give examples
        for word in ("frontend", "backend", "browser", "localStorage", "HTTP"):
            assert word not in text_of_steps, f"{name}: a step names {word}"
    assert not re.search(r"\b\d+ to \d+\b|at most \d+|at least \d+", text_of_steps), f"{name}: fixed count"


def test_every_input_a_runbook_declares_is_used():
    for name, text in shipped():
        rb = load_runbook(RUNBOOKS_DIR / f"{name}.yaml")
        used = input_refs(text) | {m for m in re.findall(r"inputs\.([a-z_]+)", text)}
        assert set(rb.inputs) <= used, f"{name}: unused inputs {set(rb.inputs) - used}"
