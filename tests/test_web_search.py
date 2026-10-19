"""Original v5 Brave/DDG checks with local storage instead of experiment logs."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from ddgs.http_client import HttpClient

from ascend_github.core.storage import ToolStorage
from ascend_github.core.tool_web import BRAVE_SEARCH_URL, WebTools

SECRET = "test-brave-private-key"


class ObservedStorage(ToolStorage):
    def __init__(self, root):
        self.events = []
        super().__init__(root, observer=lambda kind, **data: self.events.append({"kind": kind, **data}))


def search_response(query="topic"):
    return {"type": "search", "query": {"original": query, "more_results_available": True},
            "web": {"results": [{"title": "Evidence", "url": "https://example.org/evidence",
                                 "description": "Summary", "extra_snippets": ["More evidence"]}]}}


@pytest.mark.asyncio
async def test_brave_parameters_raw_evidence_and_fetch_auth_boundary(tmp_path):
    seen = []

    async def transport(request):
        seen.append(request)
        if request.url.host == "api.search.brave.com":
            assert request.headers["x-subscription-token"] == SECRET
            return httpx.Response(200, json=search_response(), headers={"x-ratelimit-remaining": "9, 99"})
        assert "x-subscription-token" not in request.headers
        return httpx.Response(200, text="<h1>Verified source</h1>", headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        tools = WebTools(client, ObservedStorage(tmp_path), brave_api_key=SECRET, search_interval=0)
        result = await tools.web_search("s", [{"query": "topic", "max_results": 7, "page": 3,
                                             "region": "cn-zh", "timelimit": "m"}])
        await tools.web_fetch("f", [{"url": "https://example.org/evidence"}])
    assert dict(seen[0].url.params) == {"q": "topic", "count": "7", "offset": "2", "country": "CN",
        "search_lang": "zh-hans", "freshness": "pm", "result_filter": "web", "text_decorations": "false",
        "spellcheck": "false", "extra_snippets": "true"}
    item = result["results"][0]
    assert item["backend"] == "brave" and item["returned_count"] == 1
    assert item["items"][0] == {"title": "Evidence", "href": "https://example.org/evidence",
                                 "body": "Summary", "extra_snippets": ["More evidence"]}
    assert item["query_info"]["more_results_available"] is True
    assert tools.metadata()["engine"] == "brave"
    bodies = list(tmp_path.rglob("*.body"))
    assert any(json.loads(p.read_text()) == search_response() for p in bodies if p.read_text().startswith("{"))
    assert all(SECRET.encode() not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


@pytest.mark.asyncio
@pytest.mark.parametrize(("headers", "delay"), [({"retry-after": "15"}, 15),
    ({"x-ratelimit-remaining": "0, 5", "x-ratelimit-reset": "2, 3600"}, 2),
    ({"x-ratelimit-limit": "50, 0", "x-ratelimit-remaining": "0, 0", "x-ratelimit-reset": "1, 406514"}, 1),
    ({}, 60)])
async def test_brave_429_preserves_failure_and_stops_queued_requests(tmp_path, headers, delay):
    seen = []

    async def transport(request):
        seen.append(request)
        return httpx.Response(429, json={"error": {"code": "RATE_LIMITED"}}, headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        tools = WebTools(client, ObservedStorage(tmp_path), brave_api_key=SECRET, search_interval=0)
        result = await tools.web_search("s", [{"query": "one"}, {"query": "two"}])
    first, second = result["results"]
    assert len(seen) == 1 and first["error"]["status"] == 429
    assert first["error"]["retry_after"] == delay
    assert first["error"]["service_code"] == "RATE_LIMITED"
    assert "no request sent" in second["error"]["message"]
    assert (tmp_path / first["error"]["body_file"]).is_file()
    assert all("items" not in item for item in result["results"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "payload"), [(401, {"error": "unauthorized"}),
    (302, {}), (503, {}), (200, {"error": "unexpected error envelope"}), (200, "not JSON")])
async def test_brave_failures_are_not_empty_success_or_redirects(tmp_path, status, payload):
    seen = []

    async def transport(request):
        seen.append(request)
        assert str(request.url).startswith(BRAVE_SEARCH_URL)
        return httpx.Response(status, json=payload, headers={"location": "https://example.org/redirect"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        result = await WebTools(client, ObservedStorage(tmp_path), brave_api_key=SECRET).web_search("s", [{"query": "q"}])
    item = result["results"][0]
    assert len(seen) == 1 and item["error"]["status"] == status and "items" not in item


def test_backend_selection_preserves_ddg_and_a_single_generic_prompt(tmp_path):
    client = httpx.AsyncClient()
    ddg = WebTools(client, ObservedStorage(tmp_path))
    brave = WebTools(client, ObservedStorage(tmp_path), brave_api_key=SECRET)
    explicit_ddg = WebTools(client, ObservedStorage(tmp_path), brave_api_key=SECRET, search_backend="duckduckgo")
    assert ddg.search_backend == explicit_ddg.search_backend == "duckduckgo"
    assert brave.search_backend == "brave"
    assert ddg.tool_specs == brave.tool_specs == explicit_ddg.tool_specs
    with pytest.raises(ValueError, match="BRAVE_SEARCH_API_KEY"):
        WebTools(client, ObservedStorage(tmp_path), search_backend="brave")


@pytest.mark.asyncio
async def test_search_global_concurrency_pacing_and_real_ddgs_parser(tmp_path, monkeypatch):
    active = peak = 0
    requests = []

    def request(self, method, url, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        query = kwargs["data"]["q"]
        requests.append((method, url, query))
        time.sleep(0.01)
        active -= 1
        html = f'<div class="body"><h2>{query}</h2><a href="https://example.org/{query}">{query} snippet</a></div>'
        return SimpleNamespace(status_code=200, content=html.encode(), text=html)

    monkeypatch.setattr(HttpClient, "request", request)
    async with httpx.AsyncClient() as client:
        tools = WebTools(client, ObservedStorage(tmp_path), search_interval=0.03)
        first, second = await asyncio.gather(
            tools.web_search("a", [{"query": "one"}, {"query": "two"}]),
            tools.web_search("b", [{"query": "three"}]),
        )
    assert peak == 1 and len(requests) == 3
    assert all(method == "POST" and url == "https://html.duckduckgo.com/html/" for method, url, _ in requests)
    assert [item["items"][0]["title"] for item in first["results"]] == ["one", "two"]
    assert second["results"][0]["items"][0]["href"] == "https://example.org/three"
    assert len(list(tmp_path.rglob("*.body"))) == 3
    assert any(e["kind"] == "web/search_wait" for e in tools.storage.events)


@pytest.mark.asyncio
async def test_search_202_is_preserved_and_queued_searches_cool_down(tmp_path, monkeypatch):
    received = []

    def request(self, *args, **kwargs):
        received.append(args)
        return SimpleNamespace(status_code=202, content=b"challenge", text="challenge")

    monkeypatch.setattr(HttpClient, "request", request)
    async with httpx.AsyncClient() as client:
        result = await WebTools(client, ObservedStorage(tmp_path), search_interval=0).web_search(
            "a", [{"query": "first"}, {"query": "second"}])
    assert len(received) == 1
    first, second = result["results"]
    assert first["error"]["status"] == 202 and first["error"]["rate_limited"]
    assert (tmp_path / first["error"]["body_file"]).read_bytes() == b"challenge"
    assert "no request sent" in second["error"]["message"]
    assert all("items" not in item for item in result["results"])


@pytest.mark.asyncio
async def test_cancelled_search_keeps_permit_until_worker_stops(tmp_path, monkeypatch):
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    calls = []

    def search(self, operation, query):
        calls.append(query["query"])
        if len(calls) == 1:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3)
        return []

    monkeypatch.setattr(WebTools, "_search", search)
    async with httpx.AsyncClient() as client:
        tools = WebTools(client, ObservedStorage(tmp_path), search_interval=0)
        first = asyncio.create_task(tools.web_search("a", [{"query": "first"}]))
        await asyncio.wait_for(started.wait(), 2)
        first.cancel()
        second = asyncio.create_task(tools.web_search("b", [{"query": "second"}]))
        try:
            await asyncio.sleep(0.02)
            assert calls == ["first"] and not first.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
    assert calls == ["first", "second"]
    assert any(e.get("error", {}).get("type") == "CancelledError" for e in tools.storage.events)
