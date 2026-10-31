"""A role agent: one A2A server that does any task for one role, on the worker and model it
is staffed with.

  python -m factory.agent ROLE --worker W [--model M] [--port N]

The orchestrator sends a task; the agent builds the prompt from its role and the task, runs
its LLM in the task's directory with the role's tools, and returns the JSON the task asks for,
validated against that schema (with one retry when the reply is unusable). The orchestrator
validates it again, and runs the gates; the agent never grades its own work.

Request (JSON data part):
  {task, item, instructions, stack, context: [{title, body}], output, schema_ctx,
   commands, cwd, feedback, interactive, checkpoint}

Replies:
  final      {output, usage}
  questions  {kind: "questions", questions, output, usage}   answer {answers: [str]}
  review     {kind: "review", output, rendered, usage}       answer {accept: true} or
                                                             {accept: false, changes: str}

`usage` lists the LLM calls made for the reply: worker, model, tokens and cost (see invoke.py).
The final reply lists the calls of every draft in the conversation. A failed task carries the
`usage` it spent in its error message.

`interactive` lets a role that `asks` put open questions to the user; `checkpoint` makes the
user approve the output before the task completes. Either way the conversation stays on one
A2A task, and each new draft is made with the whole conversation so far.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
from pathlib import Path

import uvicorn

from . import schemas
from .a2a_transport import Reply, TaskError, Turn, agent_card, build_app
from .config import Config, load_config
from .invoke import LLMFailed, Usage, invoke
from .paths import LLM_CONFIG
from .roles import ROLES, Role

LLM_ATTEMPTS = 2


def extract_json(text: str) -> object:
    """The JSON value in the last ```json block of `text`, or `text` itself if it is JSON."""
    blocks = re.findall(r"```(?:json)?[ \t]*\n(.*?)```", text, re.S)
    for block in reversed(blocks):
        try:
            return json.loads(block)
        except ValueError:
            continue
    try:
        return json.loads(text.strip())
    except ValueError:
        raise ValueError("the reply had no ```json block with valid JSON") from None


def prompt_for(role: Role, req: dict, history: str) -> str:
    parts = [f"You are the {role.title.lower()}. {role.description}", role.prompt]
    if req.get("stack"):
        parts.append(f"Project stack (fixed; do not change it):\n{req['stack']}")
    parts.append(f"Your task:\n{req['instructions'].strip()}")
    for c in req.get("context", []):
        parts.append(f"{c['title']}:\n{c['body']}")
    if req.get("feedback"):
        parts.append(f"Feedback on your previous attempt (fix this):\n{req['feedback']}")
    if history:
        parts.append(history)
    if name := req.get("output"):
        parts.append("When you are done, end your reply with exactly one ```json code block, and "
                     f"nothing after it, with this shape:\n{schemas.SCHEMAS[name].shape}")
    return "\n\n".join(parts)


def conversation(turns: list[Turn]) -> str:
    """Our earlier drafts and the user's answers, for the next draft."""
    out = []
    for ask, answer in zip(turns[1::2], turns[2::2]):
        ask, answer = ask["data"], answer["data"]
        out.append(f"Your previous draft:\n```json\n{json.dumps(ask['output'], indent=2)}\n```")
        if ask["kind"] == "questions":
            qa = [f"Q: {q}\nA: {a}" for q, a in zip(ask["questions"], answer.get("answers", []))]
            out.append("The user's answers (apply them, and drop the answered questions):\n" + "\n".join(qa))
        else:
            out.append(f"The user asked for these changes:\n{answer.get('changes', '')}")
    return "\n\n".join(out)


def produce(cfg: Config, role: Role, worker: str, model: str, req: dict,
            history: str) -> tuple[dict, list[Usage]]:
    """The validated output and the usage of every LLM call made for it. A failure is a TaskError
    that carries the usage."""
    tools = role.tools(req.get("commands", {}))
    cwd = Path(req["cwd"])
    retry, spent = "", []
    for _ in range(LLM_ATTEMPTS):
        try:
            out, used = invoke(cfg, worker, model, prompt_for(role, req, history + retry), cwd, tools)
        except LLMFailed as e:
            raise TaskError(f"{type(e).__name__}: {e}", {"usage": [*spent, e.usage]}) from e
        spent.append(used)
        if not req.get("output"):
            return {"summary": out.strip()[-1500:]}, spent
        try:
            return schemas.validate(req["output"], extract_json(out), req.get("schema_ctx")), spent
        except ValueError as e:
            retry = f"\n\nYour previous reply was rejected: {e}. Follow the reply format exactly."
    raise TaskError(f"RuntimeError: no usable {req['output']} after {LLM_ATTEMPTS} attempts:{retry}",
                    {"usage": spent})


def handle(cfg: Config, role: Role, worker: str, model: str, turns: list[Turn]) -> Reply:
    req, last = turns[0]["data"], turns[-1]["data"]
    # The calls made for the drafts that were already sent back to the caller.
    earlier = [u for t in turns if t["role"] == "agent" for u in t["data"].get("usage", [])]
    if len(turns) > 1 and turns[-2]["data"]["kind"] == "review" and last.get("accept"):
        return Reply({"output": turns[-2]["data"]["output"], "usage": earlier})
    try:
        output, spent = produce(cfg, role, worker, model, req, conversation(turns))
    except TaskError as e:
        raise TaskError(str(e), {"usage": [*earlier, *e.data["usage"]]}) from e
    if role.asks and req.get("interactive") and output.get("questions"):
        return Reply({"kind": "questions", "questions": output["questions"], "output": output,
                      "usage": spent}, final=False)
    if req.get("checkpoint"):
        rendered = schemas.render(req.get("output"), output)
        return Reply({"kind": "review", "output": output, "rendered": rendered, "usage": spent}, final=False)
    return Reply({"output": output, "usage": [*earlier, *spent]})


def card_for(role: Role, worker: str, model: str, url: str):
    return agent_card(
        name=f"{role.name}-agent", description=role.description, url=url,
        skill_id=role.name, skill_name=role.title,
        tags=[f"role:{role.name}", f"worker:{worker}", f"model:{model}"],
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve one factory role as an A2A agent.")
    ap.add_argument("role", choices=sorted(ROLES))
    ap.add_argument("--worker", required=True, help="a worker in llm.yaml")
    ap.add_argument("--model", help="one of the worker's models (default: its default model)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--config", type=Path, default=LLM_CONFIG)
    args = ap.parse_args()

    cfg = load_config(args.config)
    model = cfg.check_request(args.worker, args.model)
    role = ROLES[args.role]
    url = f"http://{args.host}:{args.port}"
    card = card_for(role, args.worker, model, url)
    print(f"{card.name} ({args.worker} / {model}) serving A2A at {url}", flush=True)
    app = build_app(functools.partial(handle, cfg, role, args.worker, model), card)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
