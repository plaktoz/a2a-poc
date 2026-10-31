"""Runbooks as data: loading, validation, trait composition and staffing (no LLM involved)."""

from pathlib import Path

import pytest
import yaml

from factory import ConfigError, load_config
from factory.paths import ROOT, RUNBOOKS_DIR
from factory.runbook import Runbook, catalog, holds, load_runbook, load_staff, mermaid, outline, resolve
from factory.runbook import fill
from test_config import ENV, base, write

AGENTS = ROOT / "agents.yaml"


@pytest.fixture
def cfg(tmp_path):
    data = base()
    data["workers"]["codex-gemini"]["enabled"] = True
    return load_config(write(tmp_path, data), ENV)


@pytest.fixture
def staff(cfg):
    return load_staff(AGENTS, cfg)


def plan_for(name, cfg, staff, **traits):
    return resolve(catalog()[name], traits, staff, cfg)


def ids(plan):
    return [s.id for s in plan.steps()]


def test_shipped_runbooks_load():
    books = catalog()
    assert set(books) == {"web-app", "research", "code-audit", "api-design"}
    for p in RUNBOOKS_DIR.glob("*.yaml"):
        assert load_runbook(p).name == p.stem


def test_frontend_only_is_the_default_web_app(cfg, staff):
    plan = plan_for("web-app", cfg, staff)
    assert plan.surfaces == ["web"] and plan.traits == {"frontend": True, "backend": False}
    assert ids(plan) == ["spec", "tests", "develop", "verify"]
    assert "localStorage" in plan.notes["web"]


def test_backend_only_web_app_has_no_ui_and_no_contract(cfg, staff):
    plan = plan_for("web-app", cfg, staff, frontend=False, backend=True)
    assert plan.surfaces == ["api"] and "contract" not in ids(plan)
    assert plan.templates["api"].suite == "pytest"


def test_full_stack_adds_the_contract_and_both_scaffolds(cfg, staff):
    plan = plan_for("web-app", cfg, staff, backend=True)
    assert plan.surfaces == ["web", "api"]
    assert ids(plan) == ["spec", "contract", "tests", "develop", "verify"]
    assert "mock fetch" in plan.notes["web"]
    assert {"analyst", "architect", "tester", "developer", "verifier"} == set(plan.roles())


def test_a_web_app_needs_at_least_one_surface(cfg, staff):
    with pytest.raises(ConfigError, match="no scaffold"):
        plan_for("web-app", cfg, staff, frontend=False)


def test_research_writes_no_code_and_can_skip_review(cfg, staff):
    plan = plan_for("research", cfg, staff)
    assert plan.surfaces == [] and ids(plan) == ["plan", "investigate", "report", "review"]
    assert "developer" not in plan.roles()
    assert "review" not in ids(plan_for("research", cfg, staff, review=False))


def test_unknown_trait_is_rejected(cfg, staff):
    with pytest.raises(ConfigError, match="no trait mobile"):
        plan_for("web-app", cfg, staff, mobile=True)


def test_checker_must_not_share_the_authors_model(cfg, staff):
    from dataclasses import replace

    same = {**staff, "verifier": replace(staff["verifier"], worker="codex-gpt", model="gpt-6.1-sol")}
    with pytest.raises(ConfigError, match="verifier and the developer"):
        resolve(catalog()["web-app"], {}, same, cfg)


def test_staff_overrides_and_unknown_models(cfg):
    from factory.runbook import StaffDef
    from factory import ModelNotAllowed

    s = load_staff(AGENTS, cfg, {"writer": StaffDef(worker="codex-gpt")})
    assert (s["writer"].worker, s["writer"].model) == ("codex-gpt", "gpt-6.1-sol")
    with pytest.raises(ModelNotAllowed):
        load_staff(AGENTS, cfg, {"writer": StaffDef(worker="codex-gpt", model="claude-test")})


def test_outline_and_mermaid_show_every_step(cfg, staff):
    plan = plan_for("web-app", cfg, staff, backend=True)
    text, chart = outline(plan), mermaid(plan)
    for step in ids(plan):
        assert step in text and step in chart
    assert "you approve it" in text and "gpt-6.1-sol" in text


@pytest.mark.parametrize("expr, traits, want", [
    ("a", {"a": True}, True),
    ("not a", {"a": True}, False),
    ("a and not b", {"a": True, "b": False}, True),
    ("(a or b) and not c", {"a": False, "b": True, "c": True}, False),
    (None, {}, True),
])
def test_conditions(expr, traits, want):
    assert holds(expr, traits) is want


def test_conditions_are_not_code():
    with pytest.raises(ValueError):
        holds("__import__('os')", {})


def test_fill_templates():
    assert fill("{{ item.id }}: {{ files }}", {"item": {"id": "Q1"}, "files": ["a", "b"]}) == "Q1: a, b"
    with pytest.raises(ConfigError):
        fill("{{ nope }}", {})


MINIMAL = {
    "name": "x", "title": "X", "description": "d", "use_when": "u",
    "traits": {"t": {"description": "t"}},
    "steps": [{"id": "a", "role": "analyst", "instructions": "do {{ brief }}", "output": "spec"}],
}


def book(**changes):
    data = yaml.safe_load(yaml.safe_dump(MINIMAL))
    for path, value in changes.items():
        *keys, last = path.split("__")
        node = data
        for k in keys:
            node = node[int(k)] if k.isdigit() else node[k]
        node[last] = value
    return Runbook.model_validate(data)


@pytest.mark.parametrize("change, error", [
    ({"steps__0__role": "wizard"}, "unknown role"),
    ({"steps__0__output": "poem"}, "unknown output"),
    ({"steps__0__gates": ["vibes"]}, "unknown gate"),
    ({"steps__0__when": "mobile"}, "unknown trait"),
    ({"steps__0__reads": ["later"]}, "no earlier step"),
    ({"steps__0__instructions": "{{ secret }}"}, "unknown template variable"),
    ({"steps__0__writes": "source"}, "only a step working in a surface"),
    ({"steps__0__for_each": "surfaces"}, "no scaffolds"),
    ({"steps__0__cwd": "target"}, "needs_target"),
    ({"steps__0__surprise": 1}, "Extra inputs"),
])
def test_bad_runbooks_are_rejected_with_a_reason(change, error):
    with pytest.raises(ValueError, match=error):
        book(**change)


def test_minimal_runbook_is_valid():
    assert book().steps[0].cwd == "workspace"
