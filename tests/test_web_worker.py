"""The native bridge preserves batch failures, image capability and saved refs."""

import asyncio
import io
import json
from pathlib import Path

import httpx
from PIL import Image

from ascend_github.core.storage import ToolStorage
from ascend_github.web_worker import FetchSession, definition


def test_native_schema_matches_original_web_fetch():
    path = Path(__file__).resolve().parents[1] / "assets/web-fetch.json"
    assert json.loads(path.read_text()) == definition()


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
