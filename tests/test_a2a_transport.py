import asyncio
import socket
import threading
import time

import pytest
import uvicorn

from factory.a2a_transport import Reply, StageFailed, agent_card, build_app, call, fetch_card


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def serve():
    """serve(handler) -> url of a live A2A agent running `handler`."""
    servers = []

    def start(handler):
        port = _free_port()
        url = f"http://127.0.0.1:{port}"
        card = agent_card(name="test-agent", description="test agent", url=url,
                          skill_id="t", skill_name="Test", tags=["worker:w", "model:m"])  # fmt: skip
        server = uvicorn.Server(uvicorn.Config(build_app(handler, card), host="127.0.0.1",
                                               port=port, log_level="warning"))  # fmt: skip
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        servers.append((server, thread))
        return url

    yield start
    for server, thread in servers:
        server.should_exit = True
        thread.join(5)


def test_single_turn_returns_the_result(serve):
    url = serve(lambda turns: Reply({"echo": turns[-1]["data"]["x"]}))
    assert asyncio.run(call(url, {"x": "hi"})) == {"echo": "hi"}


def test_input_required_is_answered_on_the_same_task(serve):
    def handler(turns):
        if len(turns) == 1:
            return Reply({"question": "name?"}, final=False)
        return Reply({"roles": [t["role"] for t in turns], "answer": turns[-1]["data"]["answer"]})

    asked = []

    def on_input(question):
        asked.append(question)
        return {"answer": "sam"}

    url = serve(handler)
    assert asyncio.run(call(url, {"go": True}, on_input)) == {
        "roles": ["user", "agent", "user"],
        "answer": "sam",
    }
    assert asked == [{"question": "name?"}]


def test_input_required_without_on_input_raises(serve):
    url = serve(lambda turns: Reply({"question": "?"}, final=False))
    with pytest.raises(StageFailed, match="INPUT_REQUIRED"):
        asyncio.run(call(url, {}))


def test_handler_error_fails_the_task(serve):
    def handler(turns):
        raise ValueError("boom")

    url = serve(handler)
    with pytest.raises(StageFailed, match="ValueError: boom"):
        asyncio.run(call(url, {}))


def test_card_is_served_with_its_tags(serve):
    url = serve(lambda turns: Reply({}))
    card = fetch_card(url)
    assert card["name"] == "test-agent"
    assert card["skills"][0]["tags"] == ["worker:w", "model:m"]
