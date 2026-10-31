"""Usage tracking: the model, tokens and cost of every LLM call, from the CLI to run.json (no LLM)."""

import asyncio
import json
import subprocess

import pytest

from factory import load_config
from factory import invoke as inv
from factory.a2a_transport import Reply, StageFailed, TaskError, call
from factory.agent import handle, produce
from factory.engine import triage
from factory.invoke import LLMFailed, Tools, claude_result, codex_result, usage
from factory.paths import ROOT
from factory.roles import ROLES
from factory.runbook import catalog, load_staff, resolve
from test_a2a_transport import serve as _serve
from test_config import ENV, base, default_cfg, write
from test_engine import URL, Agents, make_engine, research_agents, report

serve = _serve  # the fixture from test_a2a_transport


@pytest.fixture
def cfg(tmp_path):
    data = base()
    data["workers"]["codex-gemini"]["enabled"] = True
    return load_config(write(tmp_path, data), ENV)


@pytest.fixture
def staff(cfg):
    return load_staff(ROOT / "agents.yaml", cfg)

# What `claude -p --output-format json` printed in a probe (an error call has all-zero `usage`).
CLAUDE_OK = {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
             "total_cost_usd": 0.0895,
             "usage": {"input_tokens": 2, "cache_creation_input_tokens": 17899,
                       "cache_read_input_tokens": 0, "output_tokens": 4},
             "modelUsage": {"claude-test": {"inputTokens": 2, "outputTokens": 4,
                                            "cacheReadInputTokens": 0,
                                            "cacheCreationInputTokens": 17899, "costUSD": 0.0895}}}
CLAUDE_CAPPED = {"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": None,
                 "total_cost_usd": 0.09, "usage": {"input_tokens": 0, "output_tokens": 0},
                 "modelUsage": {"claude-test": {"inputTokens": 2, "outputTokens": 45,
                                                "cacheReadInputTokens": 0,
                                                "cacheCreationInputTokens": 17903}}}


def jsonl(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


# What `codex exec --json` printed for gemini: an `error` item comes before the reply.
CODEX_OK = jsonl(
    {"type": "thread.started", "thread_id": "t"},
    {"type": "item.completed", "item": {"id": "i0", "type": "error", "message": "Model metadata not found"}},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "ok"}},
    {"type": "turn.completed", "usage": {"input_tokens": 9537, "cached_input_tokens": 100,
                                         "cache_write_input_tokens": 0, "output_tokens": 118}})


def test_claude_usage_counts_every_prompt_token_and_the_cost():
    text, u = claude_result(json.dumps(CLAUDE_OK), "claude-agent-sdk", "claude-test")
    assert text == "ok"
    assert (u["input_tokens"], u["cached_input_tokens"], u["output_tokens"]) == (17901, 0, 4)
    assert u["cost_usd"] == 0.0895 and u["worker"] == "claude-agent-sdk" and u["model"] == "claude-test"


def test_claude_error_result_has_no_text_but_keeps_what_was_spent():
    text, u = claude_result(json.dumps(CLAUDE_CAPPED), "w", "claude-test")
    assert text is None and u["cost_usd"] == 0.09 and u["input_tokens"] == 17905


def test_claude_output_that_is_not_json_has_no_text_and_no_usage():
    text, u = claude_result("Error: something", "w", "m")
    assert text is None and u["input_tokens"] == 0 and u["cost_usd"] is None


def test_codex_usage_has_tokens_and_no_cost_and_skips_the_error_item():
    text, u = codex_result(CODEX_OK, "codex-gemini", "gemini-3.6-flash")
    assert text == "ok"
    assert (u["input_tokens"], u["cached_input_tokens"], u["output_tokens"]) == (9537, 100, 118)
    assert u["cost_usd"] is None


def fake_run(monkeypatch, stdout, returncode=0):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(inv, "run", run)
    return seen


def test_invoke_asks_each_cli_for_json(monkeypatch, tmp_path):
    cfg = default_cfg()
    seen = fake_run(monkeypatch, json.dumps(CLAUDE_OK))
    text, u = inv.invoke(cfg, "claude-agent-sdk", "claude-test", "p", tmp_path, Tools())
    assert (text, u["cost_usd"]) == ("ok", 0.0895)
    assert ["--output-format", "json"] == seen[0][seen[0].index("--output-format"):][:2]
    assert "--max-budget-usd" in seen[0]  # the per-call cap is unchanged
    seen = fake_run(monkeypatch, CODEX_OK)
    text, u = inv.invoke(cfg, "codex-gpt", "gpt-6.1-sol", "p", tmp_path, Tools())
    assert text == "ok" and "--json" in seen[0] and seen[0].index("--json") < seen[0].index("--")


def test_a_failed_call_raises_with_what_it_spent(monkeypatch, tmp_path):
    fake_run(monkeypatch, json.dumps(CLAUDE_CAPPED), returncode=1)
    with pytest.raises(LLMFailed) as e:
        inv.invoke(default_cfg(), "claude-agent-sdk", "claude-test", "p", tmp_path, Tools())
    assert e.value.usage["cost_usd"] == 0.09


# --- the agent --------------------------------------------------------------


def llm(monkeypatch, *replies):
    """invoke() answers with each reply in turn; a reply that is an LLMFailed is raised."""
    from factory import agent

    queue = list(replies)

    def fake(cfg, worker, model, prompt, cwd, tools):
        r = queue.pop(0)
        if isinstance(r, LLMFailed):
            raise r
        return r

    monkeypatch.setattr(agent, "invoke", fake)


def call_of(cost, tokens=10):
    return usage("w", "m", tokens, 0, 1, cost)


def reply_of(text, cost=0.1):
    return text, call_of(cost)


VERDICT = '```json\n{"verdict": "approve"}\n```'


def req(tmp_path, **kw):
    return {"task": "review", "item": None, "instructions": "x", "context": [], "output": "verdict",
            "cwd": str(tmp_path), "commands": {}, **kw}


def test_a_retry_inside_the_agent_is_logged_as_its_own_call(monkeypatch, tmp_path):
    llm(monkeypatch, reply_of("no json here", 0.1), reply_of(VERDICT, 0.2))
    out, spent = produce(default_cfg(), ROLES["reviewer"], "w", "m", req(tmp_path), "")
    assert out["verdict"] == "approve" and [u["cost_usd"] for u in spent] == [0.1, 0.2]


def test_a_task_that_gives_up_still_reports_its_usage(monkeypatch, tmp_path):
    llm(monkeypatch, reply_of("bad", 0.1), reply_of("bad again", 0.2))
    with pytest.raises(TaskError, match="no usable verdict") as e:
        produce(default_cfg(), ROLES["reviewer"], "w", "m", req(tmp_path), "")
    assert [u["cost_usd"] for u in e.value.data["usage"]] == [0.1, 0.2]


def test_a_crashed_call_reports_the_usage_before_it(monkeypatch, tmp_path):
    llm(monkeypatch, reply_of("bad", 0.1), LLMFailed("exited 1", call_of(0.3)))
    with pytest.raises(TaskError) as e:
        produce(default_cfg(), ROLES["reviewer"], "w", "m", req(tmp_path), "")
    assert [u["cost_usd"] for u in e.value.data["usage"]] == [0.1, 0.3]


def test_the_final_reply_of_a_conversation_lists_every_draft(monkeypatch, tmp_path):
    llm(monkeypatch, reply_of(VERDICT, 0.1), reply_of(VERDICT, 0.2))
    r = req(tmp_path, checkpoint=True)
    first = handle(default_cfg(), ROLES["reviewer"], "w", "m", [{"role": "user", "data": r}])
    assert not first.final and [u["cost_usd"] for u in first.data["usage"]] == [0.1]
    turns = [{"role": "user", "data": r}, {"role": "agent", "data": first.data},
             {"role": "user", "data": {"accept": False, "changes": "again"}}]
    second = handle(default_cfg(), ROLES["reviewer"], "w", "m", turns)
    assert not second.final and [u["cost_usd"] for u in second.data["usage"]] == [0.2]
    turns += [{"role": "agent", "data": second.data}, {"role": "user", "data": {"accept": True}}]
    final = handle(default_cfg(), ROLES["reviewer"], "w", "m", turns)
    assert final.final and [u["cost_usd"] for u in final.data["usage"]] == [0.1, 0.2]


# --- the transport ----------------------------------------------------------


def test_a_failed_task_hands_its_usage_to_the_caller(serve):
    def handler(turns):
        raise TaskError("boom", {"usage": [call_of(0.5)]})

    with pytest.raises(StageFailed, match="boom") as e:
        asyncio.run(call(serve(handler), {}))
    assert e.value.data["usage"][0]["cost_usd"] == 0.5


def test_a_final_reply_carries_its_usage(serve):
    out = asyncio.run(call(serve(lambda turns: Reply({"output": {}, "usage": [call_of(0.5)]})), {}))
    assert out["usage"][0]["cost_usd"] == 0.5


# --- the engine and run.json ------------------------------------------------


class Metered(Agents):
    """Fake agents that report one call per request: Claude with a cost, others without."""

    def __call__(self, role, request, on_input):
        reply = super().__call__(role, request, on_input)
        claude = role in ("analyst", "writer")
        reply["usage"] = [usage("claude-agent-sdk" if claude else "codex-gpt",
                                "claude-test" if claude else "gpt-6.1-sol",
                                1000, 400, 50, 0.25 if claude else None)]
        return reply


def metered_run(cfg, staff, tmp_path, **kw):
    plan = resolve(catalog()["research"], {"review": False}, staff, cfg)
    base = research_agents([report(f"Yes: [JSONB]({URL}).")], [])
    agents = Metered(base.replies)
    lines = []
    engine = make_engine(plan, cfg, tmp_path, agents, **kw)
    engine.say = lines.append
    return engine, lines


def read_run(engine):
    return json.loads((engine.ws / ".factory" / "run.json").read_text())


def test_run_json_sums_tokens_and_cost_and_lists_every_call(cfg, staff, tmp_path):
    engine, lines = metered_run(cfg, staff, tmp_path)
    assert engine.run()
    run = read_run(engine)
    total = run["usage"]
    assert total["calls"] == len(run["calls"]) == 5  # plan, 2 sub-questions (one is asked twice), report
    assert total["input_tokens"] == 5000 and total["cached_input_tokens"] == 2000
    assert total["output_tokens"] == 250
    assert total["cost_usd"] == 0.5 and total["calls_without_cost"] == 3  # 2 Claude calls, 3 Codex calls
    assert set(total["by_model"]) == {"claude-test", "gpt-6.1-sol"}
    assert total["by_model"]["claude-test"]["cost_usd"] == 0.5
    assert total["by_model"]["gpt-6.1-sol"]["cost_usd"] == 0.0
    first = run["calls"][0]
    assert {"step", "item", "role", "worker", "model", "input_tokens", "cost_usd"} <= set(first)
    assert any("Usage: 5 LLM calls" in line and "3 of 5 calls report no cost" in line for line in lines)


def test_a_run_with_no_reported_usage_has_zero_calls_and_prints_no_usage_line(cfg, staff, tmp_path):
    plan = resolve(catalog()["research"], {"review": False}, staff, cfg)
    lines = []
    engine = make_engine(plan, cfg, tmp_path, research_agents([report(f"[a]({URL})")], []))
    engine.say = lines.append
    assert engine.run()
    assert read_run(engine)["usage"]["calls"] == 0 and not any("Usage:" in x for x in lines)


def test_usage_is_logged_when_a_step_fails(cfg, staff, tmp_path):
    plan = resolve(catalog()["research"], {"review": False}, staff, cfg)

    def failing(role, request, on_input):
        raise StageFailed("exited 1", {"usage": [call_of(0.4)]})

    engine = make_engine(plan, cfg, tmp_path, failing)
    assert not engine.run()
    run = read_run(engine)
    # The plan step has one retry, and each failed attempt spent 0.4.
    assert run["usage"]["cost_usd"] == 0.8 and run["calls"][0]["step"] == "plan" and len(run["calls"]) == 2


def test_triage_usage_is_counted_in_the_run(cfg, staff, tmp_path):
    spent = []

    def send(role, request, on_input):
        return {"output": {"runbook": "research", "name": "x", "rationale": "r"}, "usage": [call_of(0.05)]}

    triage(send, "b", catalog(), target=None, on_input=None, cwd=tmp_path, usage=spent)
    plan = resolve(catalog()["research"], {"review": False}, staff, cfg)
    engine = make_engine(plan, cfg, tmp_path, Metered(research_agents([report(f"[a]({URL})")], []).replies),
                         earlier_usage=spent)
    assert engine.run()
    run = read_run(engine)
    assert run["calls"][0]["step"] == "triage" and run["usage"]["calls"] == 6
    assert run["usage"]["cost_usd"] == 0.55


def test_the_log_is_safe_when_parallel_steps_report_at_once(cfg, staff, tmp_path):
    engine, _ = metered_run(cfg, staff, tmp_path)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: engine.log.add_usage("s", str(i), "r", [call_of(0.01)]), range(40)))
    assert engine.log.data["usage"]["calls"] == 40 and engine.log.data["usage"]["cost_usd"] == 0.4
