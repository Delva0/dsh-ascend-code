"""Search controls and HTTP failures survive views; quota buckets remain independent."""

import time

import httpx
import pytest

from ascend_github.core.github_api import SEARCH_FIELDS
from ascend_github.core.tool_github import GitHubRESTTool

from .helpers import RecordingStorage


async def call(tools, request):
    return (await tools.github_api("test", [request]))["results"][0]


@pytest.mark.asyncio
async def test_search_controls_survive_projection_truncation_and_old_saved_metadata(tmp_path):
    controls = {"total_count": 91, "incomplete_results": True, "search_type": "lexical",
                "lexical_fallback_reason": ["non_issue_target"]}
    original = {**controls, "items": [{"number": 2, "title": "Result"}]}
    seen = []

    async def fetch(request):
        seen.append(request)
        return httpx.Response(200, json=original)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, {"path": "/search/pulls", "params": {"q": "repo:o/r accuracy",
                                  "search_type": "semantic"}, "fields": {"n": "/number"}, "max_chars": 1})
        assert first["continue_request"] and "data" not in first
        for key, value in controls.items():
            assert first[key] == value
        # Old runs have no lifted controls, but their raw response is sufficient for recovery.
        saved = tools.responses[first["result_id"]]
        for key in SEARCH_FIELDS:
            saved.pop(key, None)
        recovered = await call(tools, {"result_id": first["result_id"], "fields": {"n": "/number"}})
        full = await call(tools, {"result_id": first["result_id"], "view": "full"})
    assert recovered["data"] == [{"n": 2}]
    assert {key: recovered[key] for key in controls} == controls
    assert full["data"] == original and len(seen) == 1


@pytest.mark.asyncio
async def test_search_collection_keeps_page_controls_and_does_not_invent_stable_counts(tmp_path):
    seen = []

    async def fetch(request):
        seen.append(request)
        page = len(seen)
        return httpx.Response(200, json={"total_count": 90 + page, "incomplete_results": page == 1,
                              "search_type": "semantic", "items": [{"number": page}]}, headers={
            "link": '<https://api.github.com/search/issues?q=repo:o/r&search_type=semantic&page=2>; rel="next"',
        } if page == 1 else {})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        result = await call(GitHubRESTTool(client, RecordingStorage(tmp_path), token=""), {
            "path": "/search/issues", "params": {"q": "repo:o/r", "search_type": "semantic"},
            "paginate": {"items_pointer": "/items", "max_pages": 2, "max_items": 10}, "fields": {"n": "/number"},
        })
    assert result["data"] == [{"n": 1}, {"n": 2}]
    assert "total_count" not in result and result["incomplete_results"] and result["search_type"] == "semantic"
    assert not result["collection"]["complete"] and result["collection"]["source_incomplete"]
    assert [page["total_count"] for page in result["collection"]["pages"]] == [91, 92]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_mode", ["semantic", "lexical"])
async def test_semantic_quota_is_separate_even_when_pr_query_falls_back(tmp_path, first_mode):
    seen = []

    async def fetch(request):
        seen.append(request)
        semantic = request.url.params.get("search_type") == "semantic"
        return httpx.Response(200, json={"items": [], "search_type": "lexical",
                                        "lexical_fallback_reason": ["non_issue_target"] if semantic else []},
                              headers={"x-ratelimit-resource": "semantic_search" if semantic else "search",
                                       "x-ratelimit-remaining": "0" if len(seen) == 1 else "5",
                                       "x-ratelimit-reset": str(time.time() + 3600)})

    first_request = {"path": "/search/pulls", "params": {"q": "repo:o/r symptoms", "search_type": first_mode}}
    other_request = {"path": "/search/issues", "params": {"q": "repo:o/r symptoms",
                     "search_type": "lexical" if first_mode == "semantic" else "semantic"}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, first_request)
        other = await call(tools, other_request)
        paused = await call(tools, first_request)
    assert first["search_type"] == "lexical" and other["status"] == 200 and len(seen) == 2
    bucket = "semantic_search" if first_mode == "semantic" else "search"
    assert set(tools.api.cooldowns) == {bucket} and paused["resource"] == bucket


@pytest.mark.asyncio
async def test_server_quota_name_is_authoritative_for_subsequent_requests(tmp_path):
    seen = []

    async def fetch(request):
        seen.append(request)
        return httpx.Response(200, json={}, headers={"x-ratelimit-resource": "custom_resource",
                              "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(time.time() + 3600)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        await call(tools, {"path": "/example"})
        paused = await call(tools, {"path": "/example"})
    assert len(seen) == 1 and set(tools.api.cooldowns) == {"custom_resource"}
    assert paused["resource"] == "custom_resource"


@pytest.mark.asyncio
async def test_validation_details_survive_fields_and_invalid_pointer(tmp_path):
    original = {"message": "Validation Failed", "errors": [{"resource": "Search", "field": "q", "code": "invalid",
                "message": "Specific filter validation detail"}], "documentation_url": "https://docs.github.com/"}
    seen = []

    async def fetch(request):
        seen.append(request)
        return httpx.Response(422, json=original)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        result = await call(tools, {"path": "/search/issues", "params": {"q": "bad"}, "fields": {"n": "/number"}})
        invalid = await call(tools, {"result_id": result["result_id"], "json_pointer": "/items"})
        full = await call(tools, invalid["view_error"]["recovery_request"])
    assert result["data"] == {} and result["error"]["errors"] == original["errors"]
    assert result["error"]["documentation_url"] == original["documentation_url"]
    assert invalid["view_error"]["code"] == "invalid_view" and invalid["error"] == result["error"]
    assert full["data"] == original and len(seen) == 1
