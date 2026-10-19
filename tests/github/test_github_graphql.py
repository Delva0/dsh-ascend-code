"""GraphQL query reuse, pagination scope and preservation of selected evidence."""

import json

import httpx
import pytest

from ascend_github.core.tool_github import GitHubGraphQLTool, GitHubRESTTool

from .helpers import RecordingStorage

QUERY = """query Search($q:String!, $after:String) {
  search(query:$q, type:ISSUE, first:2, after:$after) {
    issueCount pageInfo {hasNextPage endCursor}
    nodes {... on Issue {number title updatedAt}}
  }
  other: search(query:"repo:o/r is:issue", type:ISSUE, first:1) {issueCount}
}"""
VARIABLES = {"q": "repo:o/r is:issue A3 in:title", "after": None}
PAGINATE = {"items_pointer": "/data/search/nodes", "connection_pointer": "/data/search",
            "cursor_variable": "after", "max_pages": 2, "max_items": 3}


async def call(tools, request):
    return (await tools.github_graphql("test", [request]))["results"][0]


def page(after=None, *, count=4):
    return {"data": {"search": {
        "nodes": [{"number": n, "title": "Issue", "updatedAt": "2026-09-25"}
                  for n in ([3, 4] if after else [1, 2])],
        "issueCount": count, "pageInfo": {"hasNextPage": not after, "endCursor": "end" if after else "next"},
    }, "other": {"issueCount": 585}}}


@pytest.mark.asyncio
async def test_query_reference_reuses_exact_page_and_continues_mid_page(tmp_path):
    seen = []

    async def fetch(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert request.headers["x-github-api-version"] == "2026-03-10"
        return httpx.Response(200, json=page(payload["variables"].get("after")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubGraphQLTool(client, RecordingStorage(tmp_path), token="secret")
        first = await call(tools, {"query": QUERY, "variables": VARIABLES, "operation_name": "Search"})
        collected = await call(tools, {"query_from": first["result_id"], "paginate": PAGINATE})
        next_request = collected["collection"]["next_request"]
        assert next_request["query_from"] == collected["collection"]["pages"][-1]["result_id"]
        assert "query" not in next_request and "headers" not in next_request
        assert next_request["paginate"]["start_index"] == 1
        rest = await call(tools, next_request)
    assert len(seen) == 2  # Cached first page and cached remainder require no new HTTP.
    assert seen == [{"query": QUERY, "variables": VARIABLES, "operationName": "Search"},
                    {"query": QUERY, "variables": {**VARIABLES, "after": "next"}, "operationName": "Search"}]
    assert [i["number"] for i in collected["data"]["items"] + rest["data"]["items"]] == [1, 2, 3, 4]
    assert collected["collection"]["pages"][0]["from_result_id"]
    assert collected["collection"]["connection"] == {
        "pointer": "/data/search", "items_pointer": "/data/search/nodes", "issueCount": 4,
    }
    assert not collected["collection"]["complete"] and rest["collection"]["complete"]
    assert all("secret" not in p.read_text() for p in tmp_path.rglob("*.json"))


@pytest.mark.asyncio
async def test_next_page_reference_preserves_scope_and_changes_only_cursor(tmp_path):
    seen = []

    async def fetch(request):
        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(200, json=page(payload["variables"].get("after")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubGraphQLTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, {"query": QUERY, "variables": VARIABLES, "operation_name": "Search",
                                   "paginate": {**PAGINATE, "max_pages": 1}})
        next_request = first["collection"]["next_request"]
        assert next_request["variables"] == {"after": "next"} and "query" not in next_request
        assert "page_result_id" not in next_request["paginate"]
        last = await call(tools, next_request)
    assert len(seen) == 2 and last["collection"]["complete"]
    assert seen[1]["variables"] == {**VARIABLES, "after": "next"}
    assert not last["collection"]["pages"][0]["from_result_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"variables": {"q": "repo:o/r is:issue A3"}},
    {"headers": {"X-GitHub-Api-Version": "2022-11-28"}},
])
async def test_reference_overrides_make_fresh_requests_and_do_not_modify_source(tmp_path, override):
    seen = []

    async def fetch(request):
        seen.append(request)
        return httpx.Response(200, json=page())

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubGraphQLTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, {"query": QUERY, "variables": VARIABLES})
        changed = await call(tools, {"query_from": first["result_id"], **override,
                                     "paginate": {**PAGINATE, "max_pages": 1}})
        # Without paginate a reference always refreshes, even with no overrides.
        await call(tools, {"query_from": first["result_id"]})
        local = await call(tools, {"result_id": first["result_id"], "view": "full"})
    assert len(seen) == 3 and not changed["collection"]["pages"][0]["from_result_id"]
    assert local["request"]["json"]["variables"] == VARIABLES
    assert json.loads(seen[1].content)["variables"] == {**VARIABLES, **override.get("variables", {})}
    assert seen[1].headers["x-github-api-version"] == override.get("headers", {}).get(
        "X-GitHub-Api-Version", "2026-03-10")


@pytest.mark.asyncio
async def test_changed_document_cannot_reuse_old_page(tmp_path):
    seen = []

    async def fetch(request):
        seen.append(request)
        return httpx.Response(200, json=page())

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubGraphQLTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, {"query": QUERY, "variables": VARIABLES})
        failed = await call(tools, {"query": QUERY.replace(" updatedAt", ""), "variables": VARIABLES,
                                    "paginate": {**PAGINATE, "page_result_id": first["result_id"]}})
    assert len(seen) == 1 and failed["collection"]["stop_reason"] == "page_error"
    assert "must match" in failed["error"]["message"] and failed["data"]["items"] == []


@pytest.mark.asyncio
async def test_reference_partial_error_retry_is_fresh_and_count_changes_stay_per_page(tmp_path):
    seen = []

    async def fetch(request):
        payload = json.loads(request.content)
        seen.append(payload)
        data = page(payload["variables"].get("after"), count=4 if len(seen) < 3 else 5)
        if len(seen) == 1:
            data["errors"] = [{"message": "Temporarily inaccessible field", "type": "FORBIDDEN"}]
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubGraphQLTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, {"query": QUERY, "variables": VARIABLES})
        failed = await call(tools, {"query_from": first["result_id"], "paginate": PAGINATE})
        assert len(seen) == 1 and failed["collection"]["stop_reason"] == "page_error"
        retry = await call(tools, failed["collection"]["retry_request"])
    assert len(seen) == 3 and retry["collection"]["item_count"] == 3
    assert "issueCount" not in retry["collection"]["connection"]
    assert [p["connection_counts"]["issueCount"] for p in retry["collection"]["pages"]] == [4, 5]


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"variables": {"q": None}}, {"headers": {"Authorization": "forbidden"}},
    {"headers": {"X-Example": "one", "x-example": "two"}}, {"operation_name": "Missing"},
    {"path": "/user"}, {"query": "{viewer {login}}"}, {"result_id": "another-id"}, {"method": "GET"},
])
async def test_invalid_reference_overrides_fail_before_http(tmp_path, options):
    seen = []

    async def fetch(request):
        seen.append(request)
        return httpx.Response(200, json=page())

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubGraphQLTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, {"query": QUERY, "variables": VARIABLES})
        failed = await call(tools, {"query_from": first["result_id"], **options})
    assert len(seen) == 1 and "error" in failed


@pytest.mark.asyncio
async def test_reference_requires_graphql_response_and_revalidates_readonly(tmp_path):
    seen = []

    async def fetch(request):
        seen.append(request)
        return httpx.Response(200, json=page())

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubGraphQLTool(client, RecordingStorage(tmp_path), token="")
        rest_tool = GitHubRESTTool(client, tools.storage, token="")
        rest_tool.api.responses = tools.responses
        rest = (await rest_tool.github_rest("rest", [{"path": "/user"}]))["results"][0]
        assert "error" in await call(tools, {"query_from": rest["result_id"]})
        first = await call(tools, {"query": QUERY, "variables": VARIABLES})
        # Even restored metadata must pass the same document validation.
        tools.responses[first["result_id"]]["request"]["json"]["query"] = "mutation {anything}"
        failed = await call(tools, {"query_from": first["result_id"]})
        tools.clear_context()
        expired = await call(tools, {"query_from": first["result_id"]})
    assert len(seen) == 2 and failed["error"]["code"] == "read_only"
    assert "Unknown result_id" in expired["error"]["message"]


@pytest.mark.asyncio
async def test_graphql_selected_evidence_survives_compact_projection_and_local_windows(tmp_path):
    text = "First fixes: #16712, #16713. " + "evidence " * 100 + "Third fix: #16714."
    native = {"data": {"viewer": {"node_id": "explicit-alias", "author": {"login": "reader", "body": text}}}}
    seen = []

    async def fetch(request):
        seen.append(request)
        return httpx.Response(200, json=native if request.method == "POST" else {"body": text})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubGraphQLTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, {"query": '{viewer {node_id:id author:repository(name:"r") '
                                          '{login:name body:description}}}'})
        assert first["data"] == native and first["display_complete"] and not first["omissions"]
        assert "request" not in first and "body_file" not in first
        selected = await call(tools, {"result_id": first["result_id"], "json_pointer": "/data/viewer/author",
                                      "fields": {"comment": "/body"}})
        assert selected["data"]["comment"] == text
        window = await call(tools, {"result_id": first["result_id"], "json_pointer": "/data/viewer/author/body",
                                    "view": "compact", "max_chars": 400})
        chunks = [window["content"]]
        while window["continue_request"]:
            window = await call(tools, window["continue_request"])
            chunks.append(window["content"])
        assert json.loads("".join(chunks)) == text
        rest_tool = GitHubRESTTool(client, tools.storage, token="")
        rest = (await rest_tool.github_rest("rest", [{"path": "/user"}]))["results"][0]
    assert len(seen) == 2 and rest["data"]["body"] == text


@pytest.mark.asyncio
async def test_graphql_collections_preserve_long_strings_on_local_reread(tmp_path):
    body = "A" * 700 + " #16714"

    async def fetch(_):
        return httpx.Response(200, json={"data": {"search": {"nodes": [{"body": body}],
                                                            "pageInfo": {"hasNextPage": False}}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubGraphQLTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, {"query": QUERY, "variables": VARIABLES, "paginate": PAGINATE})
        saved = await call(tools, {"result_id": first["result_id"]})
    assert first["data"] == saved["data"] == {"items": [{"body": body}]}
    assert saved["display_complete"]
