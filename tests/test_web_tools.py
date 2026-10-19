"""Upstream v5 web-fetch checks; only experiment storage/imports are adapted."""

import asyncio
import base64
import gzip
import io
import json
import tarfile
import zipfile

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter

from ascend_github.core import tool_web as web
from ascend_github.core.storage import ToolStorage
from ascend_github.core.tool_github import GitHubRESTTool
from ascend_github.core.tool_web import WebTools


class ObservedStorage(ToolStorage):
    def __init__(self, root):
        self.events = []
        super().__init__(root, observer=lambda kind, **data: self.events.append({"kind": kind, **data}))


def png():
    output = io.BytesIO()
    Image.new("RGB", (32, 16), "red").save(output, format="PNG")
    return output.getvalue()


def zipped():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("logs/step.txt", "setup\nERROR: evidence\nfinished\n")
        archive.writestr("../escape.txt", "Never extract archive-controlled paths")
        archive.writestr("image.png", png())
    return output.getvalue()


@pytest.mark.asyncio
async def test_html_markdown_absolute_links_inline_image_and_no_credentials(tmp_path):
    requests = []
    encoded = base64.b64encode(png()).decode()
    html = (f'<html><title>Docs</title><main><h1>Heading</h1><a href="../files/log.txt">attachment</a>'
            f'<img src="/image.png" alt="diagram"><img src="data:image/png;base64,{encoded}">'
            '<pre><code>first\nsecond</code></pre><script>script-secret</script></main></html>')

    async def fetch(request):
        requests.append(request)
        assert "authorization" not in request.headers
        if request.url.host == "start.example":
            return httpx.Response(302, headers={"location": "https://docs.example/guide/page"})
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = WebTools(client, ObservedStorage(tmp_path))
        result = (await tools.web_fetch("a", [{"url": "https://start.example"}]))["results"][0]
        image = (await tools.web_fetch("b", [{"ref": result["inline_resources"][0]["ref"]}]))["results"][0]
    assert len(requests) == 2
    assert "# Heading" in result["content"] and "script-secret" not in result["content"]
    assert "https://docs.example/files/log.txt" in result["content"]
    assert "https://docs.example/image.png" in result["content"]
    assert "base64" not in result["content"] and encoded not in json.dumps(result)
    assert image["image_input"] and image["image"]["width"] == 32
    assert (tmp_path / result["body_file"]).read_text() == html


@pytest.mark.asyncio
async def test_github_redirect_to_archive_and_member_continuation(tmp_path):
    received = []

    async def fetch(request):
        received.append(request)
        if request.url.host == "api.github.com":
            assert request.headers["authorization"] == "Bearer github-secret"
            return httpx.Response(302, headers={"location": "https://downloads.example/logs?signature=temporary"})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=zipped(), headers={"content-type": "application/zip"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        log = ObservedStorage(tmp_path)
        github = GitHubRESTTool(client, log, token="github-secret")
        tools = WebTools(client, log)
        redirect = (await github.github_api("a", [{"path": "/repos/example/repo/actions/runs/1/logs"}]))["results"][0]
        assert len(received) == 1
        listing = (await tools.web_fetch("b", [{"url": redirect["redirect_url"]}]))["results"][0]
        assert listing["member_count"] == 3 and "logs/step.txt" in listing["content"]
        first = (await tools.web_fetch("c", [
            {"ref": listing["ref"], "member": "logs/step.txt", "max_lines": 2},
        ]))["results"][0]
        last = (await tools.web_fetch("d", [{"ref": listing["ref"], "member": "logs/step.txt",
                                             "start_line": first["next_line"]}]))["results"][0]
        image = (await tools.web_fetch("e", [{"ref": listing["ref"], "member": "image.png"}]))["results"][0]
        missing = (await tools.web_fetch("f", [{"ref": listing["ref"], "member": "missing"}]))["results"][0]
        tools.clear_context()
        expired = (await tools.web_fetch("g", [{"ref": listing["ref"]}]))["results"][0]
    assert len(received) == 2 and "ERROR: evidence" in first["content"]
    assert "finished" in last["content"] and last["next_line"] is None
    assert image["image_input"] and "error" in missing and "Unknown resource ref" in expired["error"]["message"]
    assert not (tmp_path.parent / "escape.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("multimodal", [True, False])
async def test_image_mode_and_binary_fallback(tmp_path, multimodal):
    async def fetch(request):
        return httpx.Response(200, content=png() if request.url.path == "/image" else b"\x00\xffbinary",
                              headers={"content-type": "application/octet-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = WebTools(client, ObservedStorage(tmp_path), is_llm_multi_modal=multimodal)
        result = await tools.web_fetch("a", [{"url": "https://example.org/image"}, {"url": "https://example.org/data"}])
        parts, observations = tools.image_parts(result, "a")
    image, binary = result["results"]
    assert image["image_input"] == multimodal and binary["format"] == "binary"
    assert "base64," not in json.dumps(result) and "base64," not in json.dumps(observations)
    if multimodal:
        assert base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1]) == png()
    else:
        assert not parts and not observations and not list(tmp_path.rglob("*.image*"))


@pytest.mark.asyncio
async def test_gzip_tar_pdf_json_and_no_implicit_cache(tmp_path):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        entry = tarfile.TarInfo("log.txt")
        entry.size = 6
        tar.addfile(entry, io.BytesIO(b"passed"))
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    pdf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(pdf)
    bodies = {"/log.gz": gzip.compress("日志\npassed".encode()), "/bundle.tar.gz": archive.getvalue(),
              "/doc.pdf": pdf.getvalue(), "/data.json": b'{"result":"passed"}'}
    received = []

    async def fetch(request):
        received.append(request)
        return httpx.Response(200, content=bodies[request.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = WebTools(client, ObservedStorage(tmp_path))
        result = await tools.web_fetch("a", [{"url": "https://example.org" + path} for path in bodies])
        compressed, tar, document, structured = result["results"]
        member = (await tools.web_fetch("b", [{"ref": tar["ref"], "member": "log.txt"}]))["results"][0]
        symlink = (await tools.web_fetch("c", [{"ref": tar["ref"], "member": "link"}]))["results"][0]
        await tools.web_fetch("d", [{"url": "https://example.org/data.json"}])
    assert len(received) == 5
    assert "日志" in compressed["content"] and "passed" in member["content"] and "error" in symlink
    assert document["pages_without_text"] == [1] and document["format"] == "pdf"
    assert '{"result":"passed"}' in structured["content"]


@pytest.mark.asyncio
async def test_download_limits_failures_and_long_line_continuation(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "MAX_DOWNLOAD_BYTES", 50000)

    async def fetch(request):
        if request.url.path == "/error":
            return httpx.Response(503, text="service failure")
        return httpx.Response(200, text="x" * (60000 if request.url.path == "/large" else 30000))

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = WebTools(client, ObservedStorage(tmp_path))
        result = await tools.web_fetch("a", [{"url": "https://example.org" + path}
                                            for path in ["/error", "/large", "/text"]])
        error, large, text = result["results"]
        rest = (await tools.web_fetch("b", [{"ref": text["ref"], "start_line": text["next_line"]}]))["results"][0]
    assert error["error"]["status"] == 503
    assert large["error"]["download_complete"] is False
    assert (tmp_path / large["error"]["body_file"]).stat().st_size == 50000
    assert text["next_line"] and len(text["content"]) < 27000 and rest["next_line"] is None
    assert (tmp_path / text["text_file"]).read_text() == "x" * 30000


@pytest.mark.asyncio
async def test_fetch_shared_concurrency_order_and_cancelled_request(tmp_path):
    active = peak = 0
    both, blocked = asyncio.Event(), asyncio.Event()

    async def fetch(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            if request.url.path == "/blocked":
                blocked.set()
                await asyncio.Event().wait()
            if active == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
            await asyncio.sleep(0.001)
            return httpx.Response(200, text=request.url.path)
        finally:
            active -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = WebTools(client, ObservedStorage(tmp_path), concurrency=2)
        one, two = await asyncio.gather(
            tools.web_fetch("a", [{"url": f"https://example.org/{i}"} for i in range(3)]),
            tools.web_fetch("b", [{"url": f"https://example.org/{i}"} for i in range(3, 5)]),
        )
        task = asyncio.create_task(tools.web_fetch("c", [{"url": "https://example.org/blocked"}]))
        await asyncio.wait_for(blocked.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not tools.fetch_limit.locked()
    assert peak == 2 and active == 0
    assert [item["content"] for item in one["results"] + two["results"]] == [f"L1: /{i}" for i in range(5)]
    events = tools.storage.events
    ended = [e for e in events if e["kind"] == "io/end"]
    assert ended[-1]["error"]["type"] == "CancelledError"
