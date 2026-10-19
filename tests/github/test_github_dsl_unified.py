"""Typed REST extensions and connection collection do not change GraphQL response shapes."""

import json

import httpx
import pytest
from graphql import GraphQLError, graphql_sync

from ascend_github.core.tool_github import GitHubDSLTool, public_schema

from .graphql_fixtures import UnifiedServer
from .helpers import RecordingStorage, call


@pytest.mark.asyncio
async def test_mixed_graphql_rest_fields_join_by_path(tmp_path):
    server = UnifiedServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        out = await call(
            GitHubDSLTool(client, RecordingStorage(tmp_path), token=""),
            """{
          repository(owner:"o",name:"r"){
            pullRequest(number:1){files(first:2){totalCount nodes{path patch viewerViewedState}}}
            file(path:"notes.md",ref:"v1"){name text}
          }
          searchCode(query:"repo:o/r needle",first:2){totalCount nodes{path url}}
          searchCommits(query:"repo:o/r needle",first:2){totalCount nodes{oid}}
        }""",
        )
    assert not out.get("errors"), out
    assert out["data"]["repository"]["pullRequest"]["files"] == {
        "totalCount": 7,
        "nodes": [{"path": f"f{i}.py", "patch": f"+line {i}", "viewerViewedState": "UNVIEWED"} for i in (1, 2)],
    }
    assert out["data"]["repository"]["file"] == {"name": "notes.md", "text": "first\nsecond\nthird\n"}
    assert out["data"]["searchCode"]["nodes"] == [
        {"path": f"f{i}.py", "url": f"https://github.com/f{i}"} for i in (1, 2)
    ]
    assert out["data"]["searchCommits"]["nodes"] == [{"oid": f"sha{i}"} for i in (1, 2)]
    assert sum(r.url.path.endswith("/files") for r in server.requests) == 1
    assert any(r.url.params.get("ref") == "v1" for r in server.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("backwards", [False, True])
async def test_bounded_collection_keeps_selected_nodes_edges_counts_and_cursors(tmp_path, backwards):
    server = UnifiedServer()
    bound = "last:2" if backwards else "first:2"
    query = (
        '{repository(owner:"o",name:"r"){issue(number:1){comments('
        + bound
        + ",limit:5){n:totalCount rows:nodes{body} edges{cursor node{body}} "
        "page:pageInfo{startCursor endCursor hasNextPage hasPreviousPage}}}}}"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        out = await call(GitHubDSLTool(client, RecordingStorage(tmp_path), token=""), query)
    assert not out.get("errors"), out
    comments = out["data"]["repository"]["issue"]["comments"]
    numbers = range(3, 8) if backwards else range(1, 6)
    assert comments["n"] == 7 and comments["rows"] == [{"body": f"comment {i}\nmore {i}\n"} for i in numbers]
    assert [e["node"] for e in comments["edges"]] == comments["rows"]
    assert comments["page"]["startCursor"] == ("2" if backwards else "0")
    assert comments["page"]["endCursor"] == ("7" if backwards else "5")
    assert len(server.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("backwards", [False, True])
async def test_collected_null_item_errors_use_indices_in_the_combined_connection(tmp_path, backwards):
    fixture = UnifiedServer()
    items = [GraphQLError(f"missing {i}") if i in (1, 3, 5) else fixture.issue(i + 1) for i in range(7)]

    async def server(request):
        root = {"search": lambda info, **args: fixture.page(items, args)}
        result = graphql_sync(public_schema(), json.loads(request.content)["query"], root_value=root)
        return httpx.Response(200, json=result.formatted)

    bound = "last" if backwards else "first"
    query = ('{hits:search(query:"repo:o/r",type:ISSUE,' + bound + ":2,limit:5)"
             "{rows:nodes{... on Issue{number}}}}")
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        out = await call(GitHubDSLTool(client, RecordingStorage(tmp_path), token=""), query)
    assert len(out["data"]["hits"]["rows"]) == 5
    assert {tuple(e["path"]) for e in out["errors"]} == {("hits", "rows", 1), ("hits", "rows", 3)}
    assert len(out["errors"]) == 2


@pytest.mark.asyncio
async def test_explicit_cursor_pagination_focuses_one_issue(tmp_path):
    server = UnifiedServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        first = await call(
            tools,
            '{repository(owner:"o",name:"r"){issue(number:1){id comments(first:2){'
            'nodes{body} pageInfo{endCursor hasNextPage}}}}}',
        )
        item = first["data"]["repository"]["issue"]
        cursor = item["comments"]["pageInfo"]["endCursor"]
        next_ = await call(
            tools,
            "query($id:ID!,$cursor:String!){node(id:$id){... on Issue{"
            "comments(first:2,after:$cursor){totalCount nodes{body}}}}}",
            variables={"id": item["id"], "cursor": cursor},
        )
        assert next_["data"]["node"]["comments"]["nodes"][0]["body"].startswith("comment 3")
        assert len(server.requests) == 2 and "repository(" not in json.loads(server.requests[-1].content)["query"]


@pytest.mark.asyncio
async def test_saved_full_data_and_structured_text_windows(tmp_path):
    server = UnifiedServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        out = await call(tools, '{repository(owner:"o",name:"r"){issue(number:1){body title}}}', max_chars=100)
        assert out["data"]["repository"]["issue"]["title"] == "title 1"
        window = out["extensions"]["display"][0]["windows"][0]
        tail = await tools.github_dsl("tail", **window["continue_request"])
        assert out["data"]["repository"]["issue"]["body"] + tail["data"]["saved"] == server.issue(1)["body"]
        full = await call(
            tools,
            'query($id:ID!){saved(id:$id,at:"/data/repository/issue",fields:["title"])}',
            variables={"id": out["extensions"]["result_id"]},
        )
        assert full["data"]["saved"] == {"title": "title 1"} and len(server.requests) == 1


@pytest.mark.asyncio
async def test_failed_collection_retains_evidence_and_reports_incomplete_page(tmp_path):
    server = UnifiedServer()
    server.fail_after = "2"
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        out = await call(
            GitHubDSLTool(client, RecordingStorage(tmp_path), token=""),
            '{search(query:"repo:o/r is:issue",type:ISSUE,first:2,limit:5){'
            'nodes{... on Issue{number}} pageInfo{hasNextPage endCursor}}}',
        )
    assert len(out["data"]["search"]["nodes"]) == 2
    assert out["data"]["search"]["pageInfo"]["hasNextPage"]
    assert out["extensions"]["pages"][0]["error"] and len(out["extensions"]["source_result_ids"]) == 2


@pytest.mark.asyncio
async def test_collection_respects_fragments_and_does_not_mutate_cached_native_page(tmp_path):
    server = UnifiedServer()
    query = """{repository(owner:"o",name:"r"){issue(number:1){comments(first:2,limit:5){...C}}}}
    fragment C on IssueCommentConnection{rows:nodes{body} page:pageInfo{...P}}
    fragment P on PageInfo{end:endCursor more:hasNextPage}"""
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        collected = await call(tools, query)
        assert not collected.get("errors"), collected
        value = collected["data"]["repository"]["issue"]["comments"]
        assert len(value["rows"]) == 5 and value["page"] == {"end": "5", "more": True}
        first = await call(tools, query.replace(",limit:5", ""))
        value = first["data"]["repository"]["issue"]["comments"]
        assert len(value["rows"]) == 2 and value["page"] == {"end": "2", "more": True}
        assert len(server.requests) == 2


@pytest.mark.asyncio
async def test_json_scalar_extension_keeps_nested_native_json(tmp_path):
    async def server(request):
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "items": [{"path": "a.py", "text_matches": [{"fragment": "needle", "matches": [{"text": "needle"}]}]}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        out = await call(
            GitHubDSLTool(client, RecordingStorage(tmp_path), token=""),
            '{searchCode(query:"needle"){nodes{textMatches}}}',
        )
        assert not out.get("errors"), out
        assert out["data"]["searchCode"]["nodes"][0]["textMatches"][0]["matches"] == [{"text": "needle"}]
