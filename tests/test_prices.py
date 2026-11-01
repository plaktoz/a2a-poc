"""Model prices (FR-30): the table prices a call whose worker reports no cost (no LLM, no network)."""

import json

import pytest

from factory import prices
from factory.invoke import price_usage
from factory.workers.base import usage

TABLE = {"models": {
    "m-big": {"input": 2.0, "cached_input": 0.1, "output": 10.0},
    "m-nocache": {"input": 1.0, "cached_input": None, "output": 4.0},
    "m-alias": {"alias_of": "m-big"},
}}
FEED = {"updated_at": "2026-10-26", "prices": [
    {"id": "m-big", "input": 3.0, "output": 12.0, "input_cached": 0.3},
    {"id": "m-nocache", "input": 1.5, "output": 5.0, "input_cached": None},
]}


def test_the_cost_splits_fresh_cached_and_output_tokens():
    # 9437 fresh x 2 + 100 cached x 0.1 + 118 out x 10, per 1M tokens
    assert prices.cost_usd("m-big", 9537, 100, 118, TABLE) == 0.020064


def test_a_model_with_no_cached_price_charges_cached_tokens_as_input():
    assert prices.cost_usd("m-nocache", 1000, 400, 0, TABLE) == 0.001


def test_an_alias_has_the_price_of_its_target():
    assert prices.cost_usd("m-alias", 1000, 0, 100, TABLE) == prices.cost_usd("m-big", 1000, 0, 100, TABLE)
    assert prices.lookup("m-alias", TABLE).model == "m-big"


def test_a_model_with_no_price_has_an_unknown_cost():
    assert prices.cost_usd("m-new", 1000, 0, 100, TABLE) is None


def test_the_shipped_file_prices_every_codex_model_and_both_aliases():
    table = prices.load_table()
    for model in ("gpt-6.1-sol", "gpt-6-luna", "gemini-3.7-flash", "gpt-advantage-assist", "claude-advantage-assist"):
        assert prices.cost_usd(model, 1000, 0, 1000, table) is not None, model
    assert prices.lookup("gpt-advantage-assist", table).model == "gpt-6.1-sol"
    assert prices.lookup("claude-advantage-assist", table).model == "claude-opus-5-5"


def test_the_shipped_models_in_llm_yaml_that_codex_runs_all_have_a_price():
    from factory.config import load_config
    from factory.paths import LLM_CONFIG
    import os
    env = {**os.environ, **{k: os.environ.get(k, "x") for k in
           ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "OPENAI_BASE_URL",
            "OPENAI_API_KEY", "GOOGLE_GEMINI_BASE_URL", "GEMINI_API_KEY")}}
    cfg = load_config(LLM_CONFIG, env)
    table = prices.load_table()
    unpriced = [(w.name, m) for w in cfg.enabled_workers() if w.plugin.endswith("codex") for m in w.models
                if prices.lookup(m, table) is None]
    assert not unpriced, f"add these to docs/model-prices.md: {unpriced}"


def test_price_usage_fills_a_missing_cost_and_keeps_a_reported_one():
    u = price_usage(usage("codex-gpt", "m-big", 1000, 0, 100, None), TABLE)
    assert u["cost_usd"] == 0.003
    assert price_usage(usage("claude", "m-big", 1000, 0, 100, 0.5), TABLE)["cost_usd"] == 0.5


def test_price_usage_leaves_an_unpriced_model_and_an_empty_call_unknown():
    assert price_usage(usage("codex-gpt", "m-new", 1000, 0, 100, None), TABLE)["cost_usd"] is None
    assert price_usage(usage("codex-gpt", "m-big"), TABLE)["cost_usd"] is None  # no tokens: nothing was spent


def test_a_broken_alias_is_refused(tmp_path):
    bad = {"models": {"a": {"alias_of": "missing"}}}
    f = tmp_path / "p.md"
    f.write_text("```json prices\n" + json.dumps(bad) + "\n```")
    with pytest.raises(prices.PriceError, match="alias"):
        prices.load_table(f)


# --- refresh ---------------------------------------------------------------------------------


def test_refresh_copies_the_feed_numbers_and_the_date():
    table = json.loads(json.dumps(TABLE))
    prices.refresh(table, FEED, "2026-11-07")
    assert table["models"]["m-big"] == {"input": 3.0, "cached_input": 0.3, "output": 12.0}
    assert table["models"]["m-nocache"]["cached_input"] is None
    assert table["models"]["m-alias"] == {"alias_of": "m-big"}
    assert (table["feed_updated_at"], table["fetched_on"]) == ("2026-10-26", "2026-11-07")


def test_refresh_uses_the_feed_id_when_the_names_differ():
    table = {"models": {"gw-name": {"feed_id": "m-big", "input": 0, "cached_input": None, "output": 0}}}
    prices.refresh(table, FEED, "2026-11-07")
    assert table["models"]["gw-name"]["input"] == 3.0


def test_refresh_changes_nothing_when_a_model_is_missing_from_the_feed():
    table = json.loads(json.dumps(TABLE))
    table["models"]["m-gone"] = {"input": 1, "cached_input": None, "output": 1}
    with pytest.raises(prices.PriceError, match="m-gone"):
        prices.refresh(table, FEED, "2026-11-07")


def test_build_replaces_the_table_and_the_block_and_keeps_the_prose():
    text = ("intro\n<!-- prices:start -->\nold\n<!-- prices:end -->\nmiddle\n"
            "```json prices\n{}\n```\nend\n")
    out = prices.build(text, TABLE)
    assert out.startswith("intro\n") and "middle" in out and out.endswith("end\n")
    assert "| `m-big` | 2 | 0.1 | 10 |" in out and "alias of `m-big`" in out
    assert json.loads(out.split("```json prices\n")[1].split("\n```")[0]) == TABLE
    assert prices.build(out, TABLE) == out  # a second refresh with the same data changes nothing


def test_the_cli_writes_the_file_from_a_saved_feed(tmp_path, monkeypatch, capsys):
    f = tmp_path / "model-prices.md"
    f.write_text("<!-- prices:start -->\n<!-- prices:end -->\n```json prices\n" + json.dumps(TABLE) + "\n```\n")
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps(FEED))
    monkeypatch.setattr(prices, "PRICES_FILE", f)
    monkeypatch.setattr(prices, "ROOT", tmp_path)
    assert prices.main(["--feed", str(feed), "--check"]) == 0
    assert "m-big: input 2.0 -> 3.0" in capsys.readouterr().out
    assert "3.0" not in f.read_text()  # --check writes nothing
    assert prices.main(["--feed", str(feed)]) == 0
    assert prices.lookup("m-big", prices.load_table(f)).input == 3.0


def test_the_cli_fails_and_writes_nothing_when_the_feed_lacks_a_model(tmp_path, monkeypatch, capsys):
    f = tmp_path / "model-prices.md"
    original = "<!-- prices:start -->\n<!-- prices:end -->\n```json prices\n" + json.dumps(TABLE) + "\n```\n"
    f.write_text(original)
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps({"updated_at": "x", "prices": FEED["prices"][1:]}))
    monkeypatch.setattr(prices, "PRICES_FILE", f)
    assert prices.main(["--feed", str(feed)]) == 1
    assert "m-big" in capsys.readouterr().err and f.read_text() == original
