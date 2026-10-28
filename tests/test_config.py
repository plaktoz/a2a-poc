import tempfile
from pathlib import Path

import pytest
import yaml

from factory import (
    ConfigError,
    ModelNotAllowed,
    claude_env,
    codex_argv,
    load_config,
)

REPO = Path(__file__).resolve().parent.parent

ENV = {
    "ANTHROPIC_BASE_URL": "https://gw.example.com",
    "ANTHROPIC_AUTH_TOKEN": "tok-claude",
    "ANTHROPIC_MODEL": "claude-test",
    "OPENAI_BASE_URL": "https://gw.example.com/v1",
    "OPENAI_API_KEY": "tok-openai",
    "GEMINI_API_KEY": "tok-gemini",
    "GOOGLE_GEMINI_BASE_URL": "https://gw.example.com/v1",
}


def base() -> dict:
    """The shipped llm.yaml with `enabled` pinned, so tests don't depend on which
    workers are switched on in the file."""
    data = yaml.safe_load((REPO / "llm.yaml").read_text())
    for name, spec in data["workers"].items():
        spec["enabled"] = name != "codex-gemini"
    return data


def write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "llm.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def load(tmp_path: Path, data: dict, env: dict | None = None):
    return load_config(write(tmp_path, data), ENV if env is None else env)


def default_cfg():
    """base(): claude-agent-sdk and codex-gpt enabled, codex-gemini disabled."""
    with tempfile.TemporaryDirectory() as d:
        return load(Path(d), base())


def test_shipped_config_loads():
    # Loads the real file as-is; which workers are enabled is up to the user.
    cfg = load_config(REPO / "llm.yaml", ENV)
    assert cfg.llm.allowed_models == ["claude-test", "gpt-6.1-sol", "gemini-3.6-flash"]
    assert set(cfg.workers) == {"claude-agent-sdk", "codex-gpt", "codex-gemini"}
    assert cfg.budgets.per_task_usd == 30
    assert cfg.budgets.per_day_usd is None


def test_pinned_workers_are_enabled_as_expected():
    cfg = default_cfg()
    assert [w.name for w in cfg.enabled_workers()] == ["claude-agent-sdk", "codex-gpt"]


def test_worker_inherits_and_overrides_globals():
    cfg = default_cfg()
    claude, gpt = cfg.worker("claude-agent-sdk"), cfg.worker("codex-gpt")
    assert (claude.base_url, claude.api_key_env) == ("https://gw.example.com", "ANTHROPIC_AUTH_TOKEN")
    assert (gpt.base_url, gpt.api_key_env, gpt.wire_api) == (
        "https://gw.example.com/v1",
        "OPENAI_API_KEY",
        "responses",
    )


def test_missing_placeholder_env_is_an_error(tmp_path):
    env = {k: v for k, v in ENV.items() if k != "ANTHROPIC_BASE_URL"}
    with pytest.raises(ConfigError, match="ANTHROPIC_BASE_URL"):
        load(tmp_path, base(), env)


def test_empty_placeholder_env_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="ANTHROPIC_MODEL"):
        load(tmp_path, base(), {**ENV, "ANTHROPIC_MODEL": ""})


def test_disabled_worker_does_not_need_its_env(tmp_path):
    env = {k: v for k, v in ENV.items() if k not in ("GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL")}
    cfg = load(tmp_path, base(), env)
    assert not cfg.workers["codex-gemini"].enabled


def test_enabled_worker_needs_its_key_env(tmp_path):
    env = {k: v for k, v in ENV.items() if k != "OPENAI_API_KEY"}
    with pytest.raises(ConfigError, match=r"OPENAI_API_KEY \(worker codex-gpt\)"):
        load(tmp_path, base(), env)


def test_enabling_gemini_requires_its_env(tmp_path):
    data = base()
    data["workers"]["codex-gemini"]["enabled"] = True
    env = {k: v for k, v in ENV.items() if k != "GEMINI_API_KEY"}
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        load(tmp_path, data, env)
    cfg = load(tmp_path, data)
    assert cfg.worker("codex-gemini").enabled


@pytest.mark.parametrize("field", ["api_key", "token", "secret"])
def test_literal_secret_fields_are_rejected(tmp_path, field):
    data = base()
    data["workers"]["codex-gpt"][field] = "sk-live-123"
    with pytest.raises(ConfigError, match="literal credentials"):
        load(tmp_path, data)


def test_literal_key_in_api_key_env_is_rejected(tmp_path):
    data = base()
    data["workers"]["codex-gpt"]["api_key_env"] = "sk-live-123"
    with pytest.raises(ConfigError, match="NAME of an environment variable"):
        load(tmp_path, data)


def test_worker_model_must_be_in_global_allowlist(tmp_path):
    data = base()
    data["workers"]["codex-gpt"]["models"] = ["gpt-9"]
    data["workers"]["codex-gpt"]["default_model"] = "gpt-9"
    with pytest.raises(ConfigError, match="gpt-9.*llm.allowed_models"):
        load(tmp_path, data)


def test_reviewer_candidate_must_be_in_global_allowlist(tmp_path):
    data = base()
    data["reviewer"]["candidates"] = ["gpt-6.1-sol", "mystery-model"]
    with pytest.raises(ConfigError, match="mystery-model"):
        load(tmp_path, data)


def test_default_model_must_be_in_worker_models(tmp_path):
    data = base()
    data["workers"]["codex-gpt"]["default_model"] = "gemini-3.6-flash"
    with pytest.raises(ConfigError, match="default_model"):
        load(tmp_path, data)


def test_global_default_model_must_be_allowed(tmp_path):
    data = base()
    data["llm"]["default_model"] = "gpt-9"
    with pytest.raises(ConfigError, match="llm.default_model"):
        load(tmp_path, data)


def test_unknown_field_is_rejected(tmp_path):
    data = base()
    data["workers"]["codex-gpt"]["base_ur"] = "x"
    with pytest.raises(ConfigError, match="base_ur"):
        load(tmp_path, data)


def test_bad_url_is_rejected(tmp_path):
    data = base()
    data["workers"]["codex-gpt"]["base_url"] = "not a url"
    with pytest.raises(ConfigError, match="URL"):
        load(tmp_path, data)


@pytest.mark.parametrize("value", [0, -5])
def test_per_task_budget_must_be_positive(tmp_path, value):
    data = base()
    data["budgets"]["per_task_usd"] = value
    with pytest.raises(ConfigError, match="per_task_usd"):
        load(tmp_path, data)


def test_disabling_all_workers_is_rejected(tmp_path):
    data = base()
    for w in data["workers"].values():
        w["enabled"] = False
    with pytest.raises(ConfigError, match="at least one worker"):
        load(tmp_path, data)


def test_reviewer_needs_two_models_when_must_differ(tmp_path):
    data = base()
    data["workers"]["codex-gpt"]["enabled"] = False
    with pytest.raises(ConfigError, match="at least two reviewer candidates"):
        load(tmp_path, data)


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml", ENV)


# ---- call-time policy -------------------------------------------------------


def test_check_request_defaults_and_accepts_listed_model():
    cfg = default_cfg()
    assert cfg.check_request("codex-gpt") == "gpt-6.1-sol"
    assert cfg.check_request("codex-gpt", "gpt-6.1-sol") == "gpt-6.1-sol"


def test_check_request_rejects_model_outside_worker_list():
    cfg = default_cfg()
    with pytest.raises(ModelNotAllowed, match="not allowed for worker"):
        cfg.check_request("codex-gpt", "claude-test")


def test_check_request_rejects_model_outside_allowlist():
    cfg = default_cfg()
    with pytest.raises(ModelNotAllowed, match="llm.allowed_models"):
        cfg.check_request("codex-gpt", "gpt-9")


def test_check_request_rejects_disabled_and_unknown_workers():
    cfg = default_cfg()
    with pytest.raises(ModelNotAllowed, match="disabled"):
        cfg.check_request("codex-gemini")
    with pytest.raises(ModelNotAllowed, match="unknown worker"):
        cfg.check_request("nope")


def test_reviewer_differs_from_author():
    cfg = default_cfg()
    assert cfg.reviewer_for("claude-test") == "gpt-6.1-sol"
    assert cfg.reviewer_for("gpt-6.1-sol") == "claude-test"


def test_reviewer_skips_models_of_disabled_workers():
    cfg = default_cfg()
    # gemini is a candidate but its worker is disabled, so it is never picked.
    assert cfg.reviewer_for("claude-test") != "gemini-3.6-flash"
    assert cfg.reviewer_for("gpt-6.1-sol") != "gemini-3.6-flash"


# ---- adapters ---------------------------------------------------------------


def test_codex_argv_uses_flags_and_never_the_key():
    cfg = default_cfg()
    argv = codex_argv(cfg, "codex-gpt", "do the thing", cd="/tmp/wt")
    assert argv[:3] == ["codex", "exec", "--ignore-user-config"]
    assert argv[-2:] == ["--", "do the thing"]
    assert ["-m", "gpt-6.1-sol"] == argv[argv.index("-m") : argv.index("-m") + 2]
    joined = "\n".join(argv)
    assert 'model_providers.factory-codex-gpt.base_url="https://gw.example.com/v1"' in joined
    assert 'model_providers.factory-codex-gpt.env_key="OPENAI_API_KEY"' in joined
    assert 'model_providers.factory-codex-gpt.wire_api="responses"' in joined
    assert "tok-openai" not in joined
    assert argv[argv.index("-C") + 1] == "/tmp/wt"


def test_codex_argv_rejects_wrong_harness_and_bad_model():
    cfg = default_cfg()
    with pytest.raises(ConfigError, match="not 'codex'"):
        codex_argv(cfg, "claude-agent-sdk", "x")
    with pytest.raises(ModelNotAllowed):
        codex_argv(cfg, "codex-gpt", "x", model="claude-test")


def test_prompt_starting_with_dash_is_not_parsed_as_a_flag():
    cfg = default_cfg()
    argv = codex_argv(cfg, "codex-gpt", "--dangerously-bypass-approvals-and-sandbox")
    assert argv[-2] == "--"


def test_claude_env():
    cfg = default_cfg()
    assert claude_env(cfg, "claude-agent-sdk", environ=ENV) == {
        "ANTHROPIC_BASE_URL": "https://gw.example.com",
        "ANTHROPIC_AUTH_TOKEN": "tok-claude",
        "ANTHROPIC_MODEL": "claude-test",
    }


def test_claude_env_rejects_wrong_harness_and_bad_model():
    cfg = default_cfg()
    with pytest.raises(ConfigError, match="not 'claude-agent-sdk'"):
        claude_env(cfg, "codex-gpt", environ=ENV)
    with pytest.raises(ModelNotAllowed):
        claude_env(cfg, "claude-agent-sdk", model="gpt-6.1-sol", environ=ENV)
