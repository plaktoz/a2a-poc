"""A2A plumbing for factory agents: serve a stage handler, and call an agent.

Requests and results are JSON objects carried in A2A data parts. A handler sees
every turn of its task so far and returns a Reply. A non-final Reply pauses the
task in input-required; `call` passes that question to `on_input` and sends the
answer back on the same task, so one task can hold a whole conversation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import (
    get_data_parts,
    new_data_message,
    new_data_part,
    new_task_from_user_message,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Part,
    Role,
    SendMessageRequest,
    TaskState,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, PROTOCOL_VERSION_1_0, TransportProtocol
from starlette.applications import Starlette

Json = dict[str, Any]
Turn = dict[str, Any]  # {"role": "user" | "agent", "data": Json}


@dataclass
class Reply:
    data: Json
    final: bool = True  # False: pause in input-required; the caller answers on the same task


Handler = Callable[[list[Turn]], Reply]


class StageFailed(Exception):
    """The agent ended the task failed, or in a state the caller cannot continue from."""


def _data(parts: list[Part]) -> Json:
    found = get_data_parts(parts)
    return found[0] if found and isinstance(found[0], dict) else {}


class _Executor(AgentExecutor):
    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self._turns: dict[str, list[Turn]] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        turns = self._turns.setdefault(task.id, [])
        turns.append({"role": "user", "data": _data(context.message.parts)})
        await updater.start_work()
        try:
            # Handlers block on LLM subprocesses for minutes; keep the event loop free.
            reply = await asyncio.to_thread(self._handler, list(turns))
        except Exception as e:
            self._turns.pop(task.id, None)
            error = {"error": f"{type(e).__name__}: {e}"}
            await updater.failed(updater.new_agent_message([new_data_part(error)]))
            return
        if reply.final:
            self._turns.pop(task.id, None)
            await updater.add_artifact([new_data_part(reply.data)], name="result")
            await updater.complete()
        else:
            turns.append({"role": "agent", "data": reply.data})
            await updater.requires_input(updater.new_agent_message([new_data_part(reply.data)]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        self._turns.pop(context.task_id or "", None)
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def agent_card(*, name: str, description: str, url: str, skill_id: str, skill_name: str,
               tags: list[str]) -> AgentCard:  # fmt: skip
    return AgentCard(
        name=name,
        description=description,
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_1_0,
            )
        ],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[AgentSkill(id=skill_id, name=skill_name, description=description, tags=tags)],
    )


def build_app(handler: Handler, card: AgentCard) -> Starlette:
    """An ASGI app serving `card` at the well-known path and A2A JSON-RPC at /."""
    request_handler = DefaultRequestHandlerV2(
        agent_executor=_Executor(handler), task_store=InMemoryTaskStore(), agent_card=card
    )
    return Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(request_handler, "/")])


def fetch_card(url: str, timeout: float = 2.0) -> Json:
    r = httpx.get(url.rstrip("/") + AGENT_CARD_WELL_KNOWN_PATH, timeout=timeout)
    r.raise_for_status()
    return r.json()


async def call(url: str, request: Json, on_input: Callable[[Json], Json] | None = None) -> Json:
    """Send `request` to the agent at `url` as a new task and return its result.

    While the task is input-required, `on_input(question)` supplies the answer.
    Raises StageFailed if the task fails, or needs input and `on_input` is None.
    """
    # No read timeout: a stage runs for minutes, and each agent bounds its own LLM calls.
    async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10)) as http:
        client = await create_client(url, ClientConfig(httpx_client=http, streaming=True))
        try:
            message = new_data_message(request, role=Role.ROLE_USER)
            while True:
                task_id = context_id = None
                state, status, result = TaskState.TASK_STATE_UNSPECIFIED, {}, None
                async for event in client.send_message(SendMessageRequest(message=message)):
                    if event.HasField("message"):
                        return _data(event.message.parts)
                    if event.HasField("task"):
                        task_id, context_id = event.task.id, event.task.context_id
                        state = event.task.status.state
                    elif event.HasField("status_update"):
                        update = event.status_update
                        task_id, context_id = update.task_id, update.context_id
                        state, status = update.status.state, _data(update.status.message.parts)
                    elif event.HasField("artifact_update"):
                        result = _data(event.artifact_update.artifact.parts)
                if state == TaskState.TASK_STATE_COMPLETED and result is not None:
                    return result
                if state == TaskState.TASK_STATE_INPUT_REQUIRED and on_input is not None:
                    message = new_data_message(
                        on_input(status), role=Role.ROLE_USER, task_id=task_id, context_id=context_id
                    )
                    continue
                raise StageFailed(status.get("error") or f"task ended in {TaskState.Name(state)}")
        finally:
            await client.close()
