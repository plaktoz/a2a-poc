"""Deliverable shapes and their checks (no LLM involved)."""

import pytest

from factory.schemas import MODELS, SHAPES, render, validate


def spec(*surfaces):
    return {"title": "t", "goal": "g", "acceptance_criteria": [
        {"id": f"AC{i + 1}", "text": "x", **({"surface": s} if s else {})} for i, s in enumerate(surfaces)]}


def test_every_schema_has_a_prompt_shape():
    assert set(SHAPES) == set(MODELS)


def test_spec_defaults_and_extra_fields_are_kept():
    out = validate("spec", {**spec(None), "priority": "high"})
    assert out["questions"] == [] and out["priority"] == "high"


def test_spec_ids_must_be_in_order():
    data = spec(None, None)
    data["acceptance_criteria"][1]["id"] = "AC3"
    with pytest.raises(ValueError, match="AC1, AC2"):
        validate("spec", data)


def test_single_surface_is_filled_in():
    out = validate("spec", spec(None, None), {"surfaces": ["web"]})
    assert {c["surface"] for c in out["acceptance_criteria"]} == {"web"}


def test_every_surface_needs_a_criterion_and_surfaces_must_exist():
    with pytest.raises(ValueError, match="no acceptance criterion for surface api"):
        validate("spec", spec("web"), {"surfaces": ["web", "api"]})
    with pytest.raises(ValueError, match="use one of"):
        validate("spec", spec("web", "mobile"), {"surfaces": ["web", "api"]})


def test_missing_fields_are_reported():
    with pytest.raises(ValueError, match="acceptance_criteria"):
        validate("spec", {"title": "t", "goal": "g"})


def test_api_contract():
    ep = {"method": "get", "path": "/api/items", "response": []}
    assert validate("api_contract", {"endpoints": [ep]})["endpoints"][0]["method"] == "GET"
    with pytest.raises(ValueError, match="/api/"):
        validate("api_contract", {"endpoints": [{**ep, "path": "/items"}]})
    with pytest.raises(ValueError, match="duplicate"):
        validate("api_contract", {"endpoints": [ep, ep]})


def test_research_plan_ids():
    qs = [{"id": "Q1", "question": "a"}, {"id": "Q3", "question": "b"}]
    with pytest.raises(ValueError, match="Q1, Q2"):
        validate("research_plan", {"title": "t", "question": "q", "sub_questions": qs})


def test_triage_checks_runbook_traits_and_name():
    ctx = {"runbooks": {"web-app": ["frontend", "backend"], "research": []}}
    ok = {"runbook": "web-app", "traits": {"backend": True}, "name": "shop-list", "rationale": "r"}
    assert validate("triage", ok, ctx)["traits"] == {"backend": True}
    for bad, msg in [({"runbook": "poem"}, "not one of"), ({"traits": {"mobile": True}}, "no trait"),
                     ({"traits": {"backend": "yes"}}, "true or false"), ({"name": "My App"}, "lowercase")]:
        with pytest.raises(ValueError, match=msg):
            validate("triage", {**ok, **bad}, ctx)


def test_verdict_is_normalized():
    assert validate("verdict", {"verdict": "APPROVE"})["verdict"] == "approve"


@pytest.mark.parametrize("name, data", [
    ("spec", spec("web")),
    ("api_contract", {"endpoints": [{"method": "POST", "path": "/api/x", "request": {"a": 1}}]}),
    ("research_plan", {"title": "t", "question": "q", "sub_questions": [{"id": "Q1", "question": "a"}]}),
    ("findings", {"sub_question_id": "Q1", "answer": "a", "findings": [
        {"claim": "c", "source_url": "https://x", "quote": "q"}]}),
    ("triage", {"runbook": "r", "traits": {"a": True}, "name": "n", "rationale": "why"}),
    ("verification", {"passed": 1, "failed": 0, "verdicts": {"AC1": "PASS"}}),
    ("verdict", {"verdict": "revise", "issues": ["x"]}),
])
def test_render_shows_the_content(name, data):
    text = render(name, validate(name, data))
    assert text and "{" not in text.splitlines()[0]
