"""The worker plugin layer: llm.yaml names a plugin by module path, and invoke() runs it (no LLM)."""

import inspect
import json

import pytest

import fake_worker
from factory import ConfigError, load_config
from factory import invoke as inv
from factory.invoke import LLMFailed, Tools, invoke
from factory.workers.base import usage
from factory.workers.loader import plugin_class
from test_config import ENV, REPO, base, default_cfg, load
from test_usage import CLAUDE_OK, CODEX_OK, fake_run


@pytest.fixture(autouse=True)
def reset_calls():
    fake_worker.CALLS.clear()
    yield
    fake_worker.CALLS.clear()


def with_fake(**worker_changes):
    """The shipped config, with codex-gpt served by the fake plugin."""
    data = base()
    data["workers"]["codex-gpt"] = {**data["workers"]["codex-gpt"], "plugin": "fake_worker",
                                    **worker_changes}
    return data


# --- the shipped config -----------------------------------------------------


def test_shipped_workers_name_the_built_in_plugins():
    cfg = load_config(REPO / "llm.yaml", ENV)
    assert {n: w.plugin for n, w in ((n, cfg.worker(n)) for n in cfg.workers)} == {
        "claude-agent-sdk": "factory.workers.claude",
        "codex-gpt": "factory.workers.codex",
        "codex-gemini": "factory.workers.codex",
    }


def test_the_harness_key_is_renamed_to_plugin(tmp_path):
    data = base()
    data["workers"]["codex-gpt"]["harness"] = "codex"
    del data["workers"]["codex-gpt"]["plugin"]
    with pytest.raises(ConfigError, match="'harness' was renamed to 'plugin'"):
        load(tmp_path, data)


def test_a_worker_must_name_a_plugin(tmp_path):
    data = base()
    del data["workers"]["codex-gpt"]["plugin"]
    with pytest.raises(ConfigError, match="plugin"):
        load(tmp_path, data)


def test_the_interface_signature_is_what_callers_use():
    assert list(inspect.signature(invoke).parameters) == [
        "cfg", "worker", "model", "prompt", "cwd", "tools"]


# --- running a plugin -------------------------------------------------------


def test_invoke_runs_the_plugin_named_by_the_worker(tmp_path):
    cfg = load(tmp_path, with_fake(options={"reply": "hello"}))
    text, spent = invoke(cfg, "codex-gpt", "gpt-6.1-sol", "say hi", tmp_path, Tools(edit=True))
    assert text == "hello"
    assert fake_worker.CALLS == [{"worker": "codex-gpt", "prompt": "say hi", "cwd": tmp_path,
                                  "tools": Tools(edit=True), "model": "gpt-6.1-sol"}]


def test_the_plugin_reports_usage_for_every_call(tmp_path):
    cfg = load(tmp_path, with_fake())
    _, spent = invoke(cfg, "codex-gpt", "gpt-6.1-sol", "p", tmp_path, Tools())
    assert spent == {"worker": "codex-gpt", "model": "gpt-6.1-sol", "input_tokens": 7,
                     "cached_input_tokens": 3, "output_tokens": 2, "cost_usd": 0.02}


def test_a_plugin_failure_keeps_its_usage(tmp_path):
    cfg = load(tmp_path, with_fake())
    with pytest.raises(LLMFailed) as e:
        invoke(cfg, "codex-gpt", "gpt-6.1-sol", "fail", tmp_path, Tools())
    assert e.value.usage["cost_usd"] == 0.02 and e.value.usage["input_tokens"] == 7


def test_a_builtin_plugin_checks_the_model_before_it_spawns_anything(monkeypatch, tmp_path):
    from factory import ModelNotAllowed

    seen = fake_run(monkeypatch, CODEX_OK)
    with pytest.raises(ModelNotAllowed):
        invoke(default_cfg(), "codex-gpt", "claude-test", "p", tmp_path, Tools())
    assert seen == []


def test_the_fake_plugin_can_be_used_for_a_claude_worker(tmp_path):
    data = base()
    data["workers"]["claude-agent-sdk"]["plugin"] = "fake_worker"
    cfg = load(tmp_path, data)
    text, _ = invoke(cfg, "claude-agent-sdk", "claude-test", "p", tmp_path, Tools())
    assert text == "fake reply" and fake_worker.CALLS[0]["worker"] == "claude-agent-sdk"


def test_the_built_in_plugins_still_produce_the_same_usage(monkeypatch, tmp_path):
    cfg = default_cfg()
    fake_run(monkeypatch, json.dumps(CLAUDE_OK))
    text, u = invoke(cfg, "claude-agent-sdk", "claude-test", "p", tmp_path, Tools())
    assert (text, u["cost_usd"]) == ("ok", 0.0895)
    fake_run(monkeypatch, CODEX_OK)
    text, u = invoke(cfg, "codex-gpt", "gpt-6.1-sol", "p", tmp_path, Tools())
    # Codex reports no cost, so invoke prices it from docs/model-prices.md: 9437 fresh x $2, 100 cached x $0.10, 118 out x $10 per 1M
    assert (text, u["cost_usd"], u["input_tokens"]) == ("ok", 0.020064, 9537)


def test_usage_helper_is_the_one_shape_every_plugin_reports():
    assert usage("w", "m") == {"worker": "w", "model": "m", "input_tokens": 0,
                               "cached_input_tokens": 0, "output_tokens": 0, "cost_usd": None}
    assert inv.usage is usage


# --- loading and validation -------------------------------------------------


def test_a_module_that_cannot_be_imported_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="'factory.workers.nope'.*cannot be imported"):
        load(tmp_path, with_fake(plugin="factory.workers.nope"))


def test_a_path_that_is_not_a_module_path_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="is not a module path"):
        load(tmp_path, with_fake(plugin="../evil.py"))


def test_a_module_without_a_plugin_class_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="does not define a class named Plugin"):
        load(tmp_path, with_fake(plugin="json"))


def test_a_plugin_that_does_not_satisfy_the_interface_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="does not satisfy the worker plugin interface.*run"):
        load(tmp_path, with_fake(plugin="fake_worker_bad"))


def test_a_disabled_worker_is_still_checked_for_a_bad_plugin(tmp_path):
    data = base()
    data["workers"]["codex-gemini"]["enabled"] = True  # keeps the reviewer rule satisfied
    data["workers"]["codex-gpt"] = {**data["workers"]["codex-gpt"], "enabled": False,
                                    "plugin": "factory.workers.nope"}
    with pytest.raises(ConfigError, match="cannot be imported"):
        load(tmp_path, data)


def test_the_plugin_validate_can_reject_the_workers_options(tmp_path):
    with pytest.raises(ConfigError, match="fake plugin has no option"):
        load(tmp_path, with_fake(options={"colour": "red"}))


def test_the_plugin_validate_passes_good_options(tmp_path):
    cfg = load(tmp_path, with_fake(options={"reply": "ok"}))
    assert cfg.worker("codex-gpt").options == {"reply": "ok"}


def test_a_builtin_plugin_takes_no_options(tmp_path):
    data = base()
    data["workers"]["codex-gpt"]["options"] = {"timeout": 5}
    with pytest.raises(ConfigError, match="takes no options; remove timeout"):
        load(tmp_path, data)


def test_plugin_loader_returns_the_plugin_class():
    cls = plugin_class("factory.workers.codex")
    assert cls.name == "codex"
    assert plugin_class("fake_worker").name == "fake"


def test_a_bad_option_value_is_a_config_error(tmp_path):
    data = with_fake(options={"reply": 5})
    with pytest.raises(ConfigError, match="must be a string"):
        load(tmp_path, data)


def test_claude_passes_a_cost_cap_only_when_llm_yaml_sets_one(monkeypatch, tmp_path):
    from factory.workers import claude as cl
    from factory import load_config
    from test_config import ENV, base, write

    def argv_for(data):
        cfg = load_config(write(tmp_path, data), ENV)
        return cl._budget_flag(cfg)

    no_cap = base()
    no_cap["budgets"].pop("per_task_usd", None)
    assert argv_for(no_cap) == []
    capped = base()
    capped["budgets"]["per_task_usd"] = 5
    assert argv_for(capped) == ["--max-budget-usd", "5.0"]
