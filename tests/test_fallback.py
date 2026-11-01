"""Fallback worker (P5, FR-7): the last retry of a step goes to the role's fallback worker, and the
independence rule still holds (no LLM, no network)."""

import pytest

from factory import ConfigError
from factory.runbook import Fallback, Staff, catalog, load_staff, resolve
from factory.roles import ROLES
from factory.runbook import StaffDef
from test_engine import PLAN, URL, Agents, findings, make_engine, report, GOOD, FAKE
from test_usage import cfg, staff  # noqa: F401  (fixtures)
from factory.paths import ROOT
import json


def write_agents(tmp_path, roles):
    p = tmp_path / "agents.yaml"
    p.write_text("roles:\n" + "".join(f"  {r}: {v}\n" for r, v in roles.items())
                 + "".join(f"  {r}: {{worker: codex-gpt}}\n" for r in ROLES if r not in roles))
    return p


def test_a_role_can_name_a_fallback_worker_and_model(cfg, tmp_path):
    p = write_agents(tmp_path, {"researcher": "{worker: claude-agent-sdk, fallback: {worker: codex-gpt, model: gpt-6.1-sol}}"})
    st = load_staff(p, cfg)
    assert st["researcher"].fallback == Fallback("codex-gpt", "gpt-6.1-sol")
    assert st["writer"].fallback is None


def test_a_fallback_must_be_allowed_and_not_the_same_as_the_main_worker(cfg, tmp_path):
    p = write_agents(tmp_path, {"researcher": "{worker: codex-gpt, fallback: {worker: codex-gpt}}"})
    with pytest.raises(ConfigError, match="same worker and model"):
        load_staff(p, cfg)


def test_the_fallback_keeps_the_independence_rule(cfg, staff, tmp_path):
    # Reviewer's fallback uses the writer's model, so resolving the plan must fail.
    p = write_agents(tmp_path, {"reviewer": "{worker: codex-gpt, fallback: {worker: claude-agent-sdk}}"})
    bad = load_staff(p, cfg)
    with pytest.raises(ConfigError, match="must use a different model"):
        resolve(catalog()["research"], {"review": True}, bad, cfg)


def test_the_last_retry_goes_to_the_fallback_and_is_accepted(cfg, staff, tmp_path):
    plan = resolve(catalog()["research"], {"review": False}, staff, cfg)
    # Give the researcher a fallback, then make the main researcher fail its first attempt.
    plan.staff["researcher"] = Staff("researcher", "claude-agent-sdk", plan.staff["researcher"].model,
                                     None, Fallback("codex-gpt", "gpt-6.1-sol"))
    main = Agents({
        ("analyst", "plan"): [PLAN],
        ("researcher", "investigate", "Q1"): [{"bad": True}, findings("Q1", GOOD, FAKE)],
        ("researcher", "investigate", "Q2"): [{"bad": True}, findings("Q2", GOOD)],
        ("writer", "report"): [report(f"[a]({URL})")],
    })
    fallback = Agents({
        ("researcher", "investigate", "Q1"): [findings("Q1", GOOD, FAKE)],
        ("researcher", "investigate", "Q2"): [findings("Q2", GOOD)],
    })
    engine = make_engine(plan, cfg, tmp_path, main, send_fallback=fallback)
    lines = []
    engine.say = lines.append
    assert engine.run()
    assert fallback.requests, "the last attempt never reached the fallback agent"
    assert all(r == "researcher" for r, _ in fallback.requests)
    assert any("last attempt on the fallback worker" in line for line in lines)


def test_no_fallback_means_no_switch(cfg, staff, tmp_path):
    plan = resolve(catalog()["research"], {"review": False}, staff, cfg)
    main = Agents({
        ("analyst", "plan"): [PLAN],
        ("researcher", "investigate", "Q1"): [{"bad": True}, findings("Q1", GOOD)],
        ("researcher", "investigate", "Q2"): [findings("Q2", GOOD)],
        ("writer", "report"): [report(f"[a]({URL})")],
    })
    fallback = Agents({})
    engine = make_engine(plan, cfg, tmp_path, main, send_fallback=fallback)
    engine.run()
    assert fallback.requests == []
