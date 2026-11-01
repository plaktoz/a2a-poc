"""`factory check`: one short prompt to each enabled model, through invoke(). No real LLM."""

import pytest

import fake_worker
from factory import check
from factory import load_config
from factory.cli import main
from test_config import base, load


@pytest.fixture(autouse=True)
def reset_calls():
    fake_worker.CALLS.clear()
    yield
    fake_worker.CALLS.clear()


def with_fake_everywhere(**options):
    """Every enabled worker served by the fake plugin, so no test calls a real model."""
    data = base()
    for name in ("claude-agent-sdk", "codex-gpt"):
        data["workers"][name] = {**data["workers"][name], "plugin": "fake_worker", "options": options}
    return data


def test_every_enabled_model_is_checked_once(tmp_path):
    cfg = load(tmp_path, with_fake_everywhere(reply="pong"))
    results = check.check_all(cfg, tmp_path)
    expected = [(w.name, m) for w in cfg.enabled_workers() for m in w.models]
    assert [(r.worker, r.model) for r in results] == expected
    assert len(expected) >= 2


def test_a_reply_passes_and_reports_its_usage(tmp_path):
    cfg = load(tmp_path, with_fake_everywhere(reply="pong"))
    [fake] = [r for r in check.check_all(cfg, tmp_path, ["codex-gpt"]) if r.model == "gpt-6.1-sol"]
    assert fake.ok and fake.detail == "pong"
    assert fake.usage["input_tokens"] == 7 and fake.usage["cost_usd"] == 0.02
    assert fake_worker.CALLS[0]["prompt"] == check.PROMPT


def test_a_failed_call_fails_with_its_error(tmp_path, monkeypatch):
    cfg = load(tmp_path, with_fake_everywhere(reply="pong"))
    monkeypatch.setattr(check, "PROMPT", "pong")
    monkeypatch.setattr(check, "PROMPT", "fail")  # the fake plugin raises on this prompt
    r = check.check_one(cfg, "codex-gpt", "gpt-6.1-sol", tmp_path)
    assert not r.ok and "fake failed" in r.detail and r.usage["input_tokens"] == 7


def test_a_model_outside_the_worker_is_a_failure_not_a_crash(tmp_path):
    cfg = load(tmp_path, with_fake_everywhere(reply="pong"))
    r = check.check_one(cfg, "codex-gpt", "gemini-3.7-flash", tmp_path)  # allowed globally, not for this worker
    assert not r.ok and "not allowed" in r.detail


def test_the_report_totals_the_check_cost_first(tmp_path):
    cfg = load(tmp_path, with_fake_everywhere(reply="pong"))
    report = check.report(check.check_all(cfg, tmp_path))
    n = sum(len(w.models) for w in cfg.enabled_workers())
    assert list(report)[:3] == ["answered", "tokens", "cost_usd"] and report["answered"] == f"{n}/{n}"
    assert set(report["models"][0]) == {"worker", "model", "ok", "detail"}  # the short default


def test_cli_check_exits_zero_only_when_every_model_answers(tmp_path, monkeypatch, capsys):
    import yaml
    cfg_file = tmp_path / "llm.yaml"
    cfg_file.write_text(yaml.safe_dump(with_fake_everywhere(reply="pong")))
    monkeypatch.setattr(check, "PROMPT", "pong")
    assert main(["--config", str(cfg_file), "check"]) == 0
    out = capsys.readouterr().out
    n = sum(len(w.models) for w in load_config(cfg_file).enabled_workers())
    assert f"answered: {n}/{n}" in out
