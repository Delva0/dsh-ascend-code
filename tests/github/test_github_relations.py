"""Convenient GraphQL reads retain sources, errors and both levels of pagination."""

import json

import httpx
import pytest
from graphql import OperationDefinitionNode, OperationType, parse

from ascend_github.core.tool_github import GitHubRESTTool

from .helpers import RecordingStorage


async def call(tools, request):
    return (await tools.github_api("test", [request]))["results"][0]


def connection(nodes, cursor=None, total=None):
    return {"nodes": nodes, "totalCount": len(nodes) if total is None else total,
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def thread(number, cursor=None):
    return {"id": f"THREAD_{number}", "isResolved": False, "isOutdated": True, "path": "src/a.py", "line": None,
            "comments": connection([{"id": f"COMMENT_{number}", "body": "Please fix this", "author": None}], cursor)}


@pytest.mark.asyncio
async def test_thread_and_comment_pages_keep_projection_context_and_recover_native_source(tmp_path):
    seen = []

    async def fetch(request):
        seen.append(request)
        assert request.url.path == "/graphql" and request.method == "POST"
        assert request.headers["authorization"] == "Bearer secret" and request.headers["x-custom"] == "value"
        payload = json.loads(request.content)
        document = parse(payload["query"])
        assert all(node.operation == OperationType.QUERY for node in document.definitions
                   if isinstance(node, OperationDefinitionNode))
        variables = payload["variables"]
        if "id" in variables:
            assert variables == {"id": "THREAD_1", "first": 1, "after": "COMMENT_CURSOR"}
            return httpx.Response(200, json={"data": {"subject": {
                "id": "THREAD_1", "isResolved": False, "isOutdated": True,
                "pullRequest": {"number": 7, "repository": {"nameWithOwner": "O/R"}},
                "items": connection([{"id": "COMMENT_2", "body": "Another comment"}]),
            }}})
        assert variables["owner"] == "o" and variables["repo"] == "r" and variables["number"] == 7
        assert variables["first"] == variables["comments"] == 1
        later = variables["after"] is not None
        if later:
            assert variables["after"] == "THREAD_CURSOR"
        return httpx.Response(200, json={"data": {"repository": {"subject": {
            "number": 7, "url": "https://github.com/o/r/pull/7", "state": "OPEN",
            "items": connection([thread(2) if later else thread(1, "COMMENT_CURSOR")],
                                None if later else "THREAD_CURSOR", 2),
        }}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="secret")
        first = await call(tools, {"path": "https://api.github.com/repos/o/r/pulls/7/review_threads?per_page=99",
            "params": {"per_page": 1, "comments_per_page": 1}, "headers": {"X-Custom": "value"},
            "fields": {"resolved": "/isResolved", "outdated": "/isOutdated"}})
        assert first["data"] == [{"resolved": False, "outdated": True}] and first["json_pointer"] == "/items"
        assert not first["connection"]["complete"] and first["connection"]["total_count"] == 2
        pending = first["connection"]["pending_comments"]
        assert pending[0]["thread_id"] == "THREAD_1"
        comments = await call(tools, pending[0]["next_request"])
        later = await call(tools, first["connection"]["next_request"])
        saved = await call(tools, {"result_id": first["result_id"], "view": "full"})
        native = await call(tools, {"result_id": first["source_result_ids"][0], "view": "full"})
    assert len(seen) == 3 and first["source_status"] == 200 and "status" not in first
    assert comments["data"]["items"] == [{"id": "COMMENT_2", "body": "Another comment"}]
    assert comments["connection"]["complete"] and later["connection"]["complete"]
    assert later["data"] == first["data"]  # The outer continuation preserves the thread projection.
    assert saved["data"]["items"][0]["comments"]["pageInfo"]["hasNextPage"]
    assert saved["connection"] == first["connection"]
    assert native["request"]["json"]["variables"]["number"] == 7
    assert "authorization" not in native["request"]["headers"]


@pytest.mark.asyncio
async def test_closing_pull_requests_include_closed_merged_prs_and_keep_page_controls(tmp_path):
    seen = []

    async def fetch(request):
        seen.append(request)
        payload = json.loads(request.content)
        assert "includeClosedPrs:true" in payload["query"]
        assert payload["variables"]["number"] == 9 and payload["variables"]["first"] == 1
        later = payload["variables"]["after"] is not None
        if later:
            assert payload["variables"]["after"] == "NEXT"
        return httpx.Response(200, json={"data": {"repository": {"subject": {
            "number": 9, "state": "CLOSED", "closedAt": "2026-09-20T00:00:00Z",
            "items": connection([{"number": 2 if later else 1, "state": "OPEN" if later else "MERGED",
                                  "merged": not later, "mergedAt": None if later else "2026-09-20T00:00:00Z"}],
                                None if later else "NEXT", 2),
        }}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, {"path": "/repos/o/r/issues/9/closing_pull_requests", "params": {"per_page": 1},
                                  "fields": {"number": "/number", "merged": "/merged"}})
        later = await call(tools, first["connection"]["next_request"])
    assert first["data"] == [{"number": 1, "merged": True}] and not first["connection"]["complete"]
    assert later["data"] == [{"number": 2, "merged": False}] and later["connection"]["complete"]
    assert len(seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("comments_missing", [False, True])
async def test_relation_partial_graphql_errors_are_not_hidden_by_projection(tmp_path, comments_missing):
    errors = [{"message": "Cannot read comment author", "path": ["repository", "subject", "items", "nodes", 0]}]
    item = thread(1)
    if comments_missing:
        item["comments"] = None

    async def fetch(_):
        return httpx.Response(200, json={"data": {"repository": {"subject": {
            "number": 7, "items": connection([item]),
        }}}, "errors": errors})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        result = await call(tools, {
            "path": "/repos/o/r/pulls/7/review_threads", "fields": {"id": "/id"},
        })
        recovered = await call(tools, {"result_id": result["result_id"], "view": "full"})
    assert result["graphql_errors"] == errors and result["partial_data"] and result["result_id"]
    assert result["error"]["code"] == "graphql_error"
    if not comments_missing:
        assert result["data"] == [{"id": "THREAD_1"}] and not result["connection"]["complete"]
        assert result["connection"]["retry_request"]["path"] == "/repos/o/r/pulls/7/review_threads"
        assert recovered["data"]["items"] == [item]
    else:
        assert recovered["data"]["data"]["repository"]["subject"]["items"]["nodes"] == [item]


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", [None, {"number": 7, "items": {
    "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None},
}}])
async def test_missing_subject_or_broken_cursor_returns_recoverable_error(tmp_path, subject):
    async def fetch(_):
        return httpx.Response(200, json={"data": {"repository": {"subject": subject}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        result = await call(tools, {"path": "/repos/o/r/pulls/7/review_threads"})
        recovered = await call(tools, result["error"]["recovery_request"])
    assert "error" in result and result["status"] == 200
    assert recovered["data"]["data"]["repository"]["subject"] == subject


@pytest.mark.asyncio
async def test_comment_thread_must_belong_to_requested_pr(tmp_path):
    async def fetch(_):
        return httpx.Response(200, json={"data": {"subject": {
            "id": "OTHER_THREAD", "pullRequest": {"number": 8, "repository": {"nameWithOwner": "o/r"}},
            "items": connection([]),
        }}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        result = await call(GitHubRESTTool(client, RecordingStorage(tmp_path), token=""), {
            "path": "/repos/o/r/pulls/7/review_threads", "params": {"thread_id": "OTHER_THREAD"},
        })
    assert "does not belong" in result["error"]["message"] and "connection" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"params": {"per_page": 101}}, {"params": {"per_page": True}}, {"params": {"per_page": [1, 2]}},
    {"params": {"page": 2}}, {"params": {"after": ""}}, {"params": {"comments_per_page": 0}},
    {"params": {"thread_id": ""}}, {"params": {"thread_id": "T", "comments_per_page": 5}},
    {"method": "HEAD"}, {"accept": "application/vnd.github.raw+json"},
    {"paginate": {"items_pointer": "/items", "max_pages": 2, "max_items": 10}},
])
async def test_invalid_relation_options_fail_before_http(tmp_path, options):
    async def fetch(_):
        pytest.fail("Invalid relation options must not reach HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        result = await call(GitHubRESTTool(client, RecordingStorage(tmp_path), token=""), {
            "path": "/repos/o/r/pulls/7/review_threads", **options,
        })
    assert "error" in result and "status" not in result and result["error"]["path"].startswith("/")
