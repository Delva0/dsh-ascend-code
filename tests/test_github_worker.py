"""Contract and lifecycle checks for the native tool's private Python bridge."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from ascend_github.core.registry import ToolRegistry
from ascend_github.core.storage import ToolStorage
from ascend_github.core.tool_github import GitHubRESTTool
from ascend_github.worker import BACKENDS, definition, dispatch, github_token, invoke


@pytest.mark.parametrize("backend", BACKENDS)
def test_native_schema_matches_python_contract(backend):
    path = Path(__file__).resolve().parents[1] / f"assets/github-{backend}.json"
    assert json.loads(path.read_text()) == definition(backend)


async def test_batch_preserves_valid_siblings_and_marks_total_failure(tmp_path):
    async def fetch(request):
        assert request.url.path == "/user"
        return httpx.Response(200, json={"login": "reader"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        registry = ToolRegistry((GitHubRESTTool(client, ToolStorage(tmp_path), token=""), "github"))
        reply = await invoke(registry, "call", {"requests": [17, {"path": "/user"}]})
        assert not reply["failed"]
        assert reply["value"]["results"][0]["error"]
        assert reply["value"]["results"][1]["data"]["login"] == "reader"
        invalid = await invoke(registry, "bad", {"requests": [17]})
        assert invalid["failed"]
        assert (await invoke(registry, "envelope", {}))["failed"]


async def test_concurrent_calls_cancel_independently_after_quiescence():
    reader = asyncio.StreamReader()
    entered = asyncio.Event()
    stopped = asyncio.Event()
    replies = asyncio.Queue()

    class Registry:
        async def call(self, name, identity, arguments):
            if identity == "slow":
                entered.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    await asyncio.sleep(0.02)
                    stopped.set()
            return {"results": [{"data": identity}]}

    task = asyncio.create_task(dispatch(reader, replies.put_nowait, Registry()))

    def send(value):
        reader.feed_data((json.dumps(value) + "\n").encode())

    try:
        send({"id": "slow", "arguments": {}})
        await asyncio.wait_for(entered.wait(), 2)
        send({"id": "fast", "arguments": {}})
        reply = await asyncio.wait_for(replies.get(), 2)
        assert reply["id"] == "fast" and not reply["failed"]
        send({"cancel": "slow"})
        reply = await asyncio.wait_for(replies.get(), 2)
        assert reply == {"id": "slow", "cancelled": True}
        assert stopped.is_set()
    finally:
        reader.feed_eof()
        await asyncio.wait_for(task, 2)


async def test_eof_drains_owned_work():
    reader = asyncio.StreamReader()
    entered = asyncio.Event()
    stopped = asyncio.Event()

    class Registry:
        async def call(self, *_):
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                stopped.set()

    task = asyncio.create_task(dispatch(reader, lambda _: None, Registry()))
    reader.feed_data(b'{"id":"one","arguments":{}}\n')
    await asyncio.wait_for(entered.wait(), 2)
    reader.feed_eof()
    await asyncio.wait_for(task, 2)
    assert stopped.is_set()


def test_credentials_use_explicit_environment_before_cli(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", " host-token ")
    monkeypatch.setenv("GITHUB_TOKEN", "lower-priority")
    monkeypatch.setattr("ascend_github.worker.subprocess.run", lambda *_a, **_k: pytest.fail("Unexpected gh"))
    assert github_token() == "host-token"
