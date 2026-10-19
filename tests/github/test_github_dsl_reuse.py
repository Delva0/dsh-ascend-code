"""DSL reuse of GraphQL selections, provenance, cache boundaries and cancellation."""

import asyncio
import json

import httpx
import pytest

from ascend_github.core.tool_github import GitHubDSLTool

from .graphql_fixtures import UnifiedServer
from .helpers import RecordingStorage, call


@pytest.mark.asyncio
async def test_search_object_and_node_share_fields_by_native_identity(tmp_path):
    server = UnifiedServer(count=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        search = await call(
            tools, '{search(query:"repo:o/r is:issue",type:ISSUE,first:2){nodes{... on Issue{number title id}}}}',
        )
        out = await call(tools, '{repository(owner:"o",name:"r"){issue(number:1){alias:title}}}')
        assert out["data"]["repository"]["issue"] == {"alias": "title 1"}
        assert len(server.requests) == 1 and out["extensions"]["evidence_reused"]
        assert set(out["extensions"]["source_result_ids"]) <= set(search["extensions"]["source_result_ids"])
        node = await call(tools, '{node(id:"I1"){... on Issue{title}}}')
        assert node["data"]["node"] == {"title": "title 1"} and len(server.requests) == 1
        extended = await call(tools, '{repository(owner:"o",name:"r"){issue(number:1){title body}}}')
        assert extended["data"]["repository"]["issue"]["body"] == server.issue(1)["body"]
        assert len(server.requests) == 2
        sent = json.loads(server.requests[-1].content)["query"]
        assert "title" not in sent and "body" in sent


@pytest.mark.asyncio
async def test_refresh_question_clear_and_saved_evidence_boundaries(tmp_path):
    server = UnifiedServer()
    query = '{repository(owner:"o",name:"r"){issue(number:1){title}}}'
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        original = await call(tools, query)
        await call(tools, query)
        assert len(server.requests) == 1
        await call(tools, query, refresh=True)
        assert len(server.requests) == 2
        tools.begin_query()
        await call(tools, query)
        assert len(server.requests) == 3
        saved = await call(
            tools, 'query($id:ID!){saved(id:$id,at:"/data")}', variables={"id": original["extensions"]["result_id"]},
        )
        assert saved["data"]["saved"] == original["data"] and len(server.requests) == 3
        tools.clear_context()
        missing = await call(
            tools, "query($id:ID!){saved(id:$id)}", variables={"id": original["extensions"]["result_id"]},
        )
        assert missing["errors"]


@pytest.mark.asyncio
async def test_list_arguments_and_search_filters_do_not_share_different_pages(tmp_path):
    server = UnifiedServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        for args in ("first:1", "first:2", 'first:1,after:"2"', "last:1"):
            out = await call(
                tools, '{repository(owner:"o",name:"r"){issue(number:1){comments(' + args + "){nodes{body}}}}}",
            )
            assert not out.get("errors"), out
        assert len(server.requests) == 4
        for word in ("alpha", "beta"):
            await call(tools, '{search(query:"repo:o/r ' + word + '",type:ISSUE,first:1){nodes{... on Issue{title}}}}')
        assert len(server.requests) == 6


@pytest.mark.asyncio
async def test_concurrent_identical_dsl_queries_share_graphql_io(tmp_path):
    server = UnifiedServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        query = '{repository(owner:"o",name:"r"){issue(number:1){title}}}'
        outputs = await asyncio.gather(*(tools.github_dsl(str(i), query) for i in range(3)))
        assert all(o["data"] == outputs[0]["data"] for o in outputs) and len(server.requests) == 1


@pytest.mark.asyncio
async def test_error_response_is_not_cached_as_success(tmp_path):
    server = UnifiedServer()
    seen = 0

    async def fetch(request):
        nonlocal seen
        seen += 1
        if seen == 1:
            return httpx.Response(200, json={"data": {"q0": None}, "errors": [{"message": "denied", "path": ["q0"]}]})
        return await server(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        query = '{repository(owner:"o",name:"r"){name}}'
        bad = await call(tools, query)
        good = await call(tools, query)
        assert bad["errors"] and not good.get("errors") and good["data"] == {"repository": {"name": "r"}}
        assert seen == 2
