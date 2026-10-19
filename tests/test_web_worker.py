"""The native bridge preserves batch failures, image capability and saved refs."""

import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from ascend_github.core.storage import ToolStorage
from ascend_github.web_worker import FetchSession, SearchSession, definition, search_options


@pytest.mark.parametrize("name", ["web_fetch", "web_search"])
def test_native_schema_matches_original_web_tool(name):
    path = Path(__file__).resolve().parents[1] / f"assets/{name.replace('_', '-')}.json"
    assert json.loads(path.read_text()) == definition(name)


async def test_bridge_refs_survive_model_switches_without_exposing_base64(tmp_path):
    png = io.BytesIO()
    Image.new("RGB", (32, 16), "red").save(png, format="PNG")
    received = []

    async def fetch(request):
        received.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, content=png.getvalue(), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        session = FetchSession(client, ToolStorage(tmp_path))
        first = await session.invoke(None, "first", {"requests": [17, {"url": "https://example.org/p.png"}]},
                                     {"image_input": False})
        assert not first["failed"] and first["value"]["results"][0]["error"]
        saved = first["value"]["results"][1]
        assert not saved["image_input"] and not first["image_parts"]
        arguments = {"requests": [{"ref": saved["ref"]}]}
        second = await session.invoke(None, "second", arguments, {"image_input": True})
        assert second["value"]["results"][0]["image_input"]
        label, image = second["image_parts"]
        assert label["text"] == "Image from web_fetch call second: https://example.org/p.png"
        assert await asyncio.to_thread(Path(image["image_url"]).read_bytes) == png.getvalue()
        assert "base64" not in json.dumps(second)
        third = await session.invoke(None, "third", arguments, {"image_input": False})
        assert not third["value"]["results"][0]["image_input"]
        assert len(received) == 1
        assert (await session.invoke(None, "bad", {"requests": [17]}, {}))["failed"]
        assert (await session.invoke(None, "bad-envelope", {}, {}))["failed"]


def test_search_credentials_and_v5_defaults(monkeypatch):
    for name in ("BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY", "DSH_ASCEND_WEB_SEARCH_BACKEND",
                 "DSH_ASCEND_WEB_SEARCH_CONCURRENCY", "DSH_ASCEND_WEB_SEARCH_INTERVAL"):
        monkeypatch.delenv(name, raising=False)
    assert search_options() == {"search_backend": "brave", "search_concurrency": 1,
                                "search_interval": 2.0, "brave_api_key": ""}
    monkeypatch.setenv("BRAVE_API_KEY", "alias-key")
    assert search_options()["brave_api_key"] == "alias-key"
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", " primary-key ")
    assert search_options()["brave_api_key"] == "primary-key"
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "  ")
    assert search_options()["brave_api_key"] == "alias-key"
    monkeypatch.setenv("DSH_ASCEND_WEB_SEARCH_BACKEND", "duckduckgo")
    monkeypatch.setenv("DSH_ASCEND_WEB_SEARCH_CONCURRENCY", "3")
    monkeypatch.setenv("DSH_ASCEND_WEB_SEARCH_INTERVAL", "0.5")
    assert search_options() == {"search_backend": "duckduckgo", "search_concurrency": 3,
                                "search_interval": 0.5, "brave_api_key": "alias-key"}


@pytest.mark.parametrize(("name", "value"), [("BACKEND", "unknown"), ("CONCURRENCY", "0"),
    ("CONCURRENCY", "1.5"), ("INTERVAL", "nan"), ("INTERVAL", "inf"), ("INTERVAL", "-1")])
def test_search_rejects_invalid_settings(monkeypatch, name, value):
    monkeypatch.setenv("DSH_ASCEND_WEB_SEARCH_" + name, value)
    with pytest.raises(ValueError):
        search_options()


async def test_search_bridge_preserves_batch_errors_and_cross_call_cooldown(tmp_path):
    received = []

    async def fetch(request):
        received.append(request)
        assert request.headers["X-Subscription-Token"] == "test-secret"
        if request.url.params["q"] == "limited":
            return httpx.Response(429, json={"error": {"code": "RATE_LIMITED"}},
                                  headers={"retry-after": "60"})
        return httpx.Response(200, json={"type": "search", "web": {"results": [
            {"title": "Evidence", "url": "https://example.org/doc", "description": "Proof"},
        ]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        session = SearchSession(client, ToolStorage(tmp_path), search_backend="brave",
                                brave_api_key="test-secret", search_interval=0)
        reply = await session.invoke(None, "one", {"queries": [17, {"query": "topic"}]}, {})
        assert not reply["failed"] and reply["value"]["results"][0]["error"]
        assert reply["value"]["results"][1]["items"][0]["title"] == "Evidence"
        assert "test-secret" not in json.dumps(reply)
        limited = await session.invoke(None, "two", {"queries": [{"query": "limited"}]}, {})
        assert limited["failed"] and limited["value"]["results"][0]["error"]["status"] == 429
        cooled = await session.invoke(None, "three", {"queries": [{"query": "later"}]}, {})
        assert cooled["failed"] and "no request sent" in json.dumps(cooled)
        assert len(received) == 2
