"""Exercise DSL validation, evidence and recoverable local views."""

import hashlib
import json
from importlib.resources import files

import httpx
import pytest
from graphql import GraphQLError, graphql_sync

from ascend_github.core.tool_github import SCHEMA_SOURCE, GitHubDSLTool, public_schema

from .graphql_fixtures import UnifiedServer
from .helpers import RecordingStorage, call


def test_upstream_schema_is_unchanged():
    path = files("ascend_github.core").joinpath("github_schema.graphql")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SCHEMA_SOURCE["sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selected",
    [
        "totalCount",
        "totalCount nodes{body}",
        "n:totalCount rows:nodes{body} page:pageInfo{hasNextPage endCursor}",
        "edges{cursor node{body}} pageInfo{hasNextPage}",
        "nodes{body} ...F",
        "pageInfo{hasNextPage} pageInfo{endCursor}",
    ],
)
async def test_selected_connections_match_upstream_graphql_exactly(tmp_path, selected):
    server = UnifiedServer()
    query = '{repository(owner:"o",name:"r"){issue(number:1){comments(first:2){' + selected + "}}}}"
    if "...F" in selected:
        query += " fragment F on IssueCommentConnection{totalCount nodes{author{login}}}"
    expected = graphql_sync(public_schema(), query, root_value={"repository": server.repo})
    assert not expected.errors
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        out = await call(GitHubDSLTool(client, RecordingStorage(tmp_path), token=""), query)
    assert not out.get("errors"), out
    assert out["data"] == expected.data
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_unions_require_explicit_fragments_and_keep_each_type(tmp_path):
    server = UnifiedServer(count=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        bad = await call(tools, '{search(query:"mixed",type:ISSUE){nodes{number}}}')
        assert bad["errors"] and not server.requests
        query = ('{search(query:"mixed",type:ISSUE,first:2){nodes{__typename '
                 '... on Issue{number title} ... on PullRequest{number state}}}}')
        result = await call(tools, query)
        assert not result.get("errors"), result
        assert result["data"]["search"]["nodes"] == [
            {"__typename": "PullRequest", "number": 1, "state": "OPEN"},
            {"__typename": "Issue", "number": 2, "title": "title 2"},
        ]


@pytest.mark.asyncio
async def test_standard_variable_coercion_and_skip_keep_native_requests_typed(tmp_path):
    server = UnifiedServer()
    query = (
        'query($n:Int!,$skip:Boolean!){repository(owner:"o",name:"r"){issue(number:$n){title body @skip(if:$skip)}}}'
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        out = await call(tools, query, variables={"n": 2, "skip": True})
        assert out["data"]["repository"]["issue"] == {"title": "title 2"}
        sent = json.loads(server.requests[0].content)["query"]
        assert "body" not in sent and "number: 2" in sent
        invalid = await call(tools, query, variables={"n": "2", "skip": True})
        assert invalid["errors"] and len(server.requests) == 1


@pytest.mark.asyncio
async def test_native_large_integer_ids_survive_aliases_cache_and_saved_reads(tmp_path):
    ids = [2**31, 108556332653, 2**60 + 1]
    seen = []

    async def server(request):
        seen.append(request)
        return httpx.Response(200, json={"data": {"q0": {
            "_graphub_type": "CheckSuite", "_graphub_id": "CS",
            "checkRuns": {"_graphub_type": "CheckRunConnection", "_graphub_count": len(ids),
                          "_graphub_page": {"hasNextPage": False, "hasPreviousPage": False,
                                            "startCursor": "start", "endCursor": "end"}, "nodes": [
                {"_graphub_type": "CheckRun", "_graphub_id": f"CR{i}", "db": value, "name": f"job{i}"}
                for i, value in enumerate(ids)
            ]},
        }}})

    query = ('{suite:node(id:"CS"){... on CheckSuite{checkRuns(first:3){nodes{...Run}}}}} '
             'fragment Run on CheckRun{db:databaseId name}')
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, query)
        assert not first.get("errors"), first
        assert [n["db"] for n in first["data"]["suite"]["checkRuns"]["nodes"]] == ids
        cached = await call(tools, query.replace("suite:node", "again:node"))
        assert not cached.get("errors") and cached["data"]["again"] == first["data"]["suite"]
        saved = await call(tools, 'query($id:ID!){saved(id:$id,at:"/data/suite")}',
                           variables={"id": first["extensions"]["result_id"]})
        assert saved["data"]["saved"] == first["data"]["suite"]
        assert len(seen) == 1
        assert "db: databaseId" in json.loads(seen[0].content)["query"]


@pytest.mark.asyncio
async def test_native_int_compatibility_keeps_input_and_local_output_validation(tmp_path):
    seen = []

    async def server(request):
        seen.append(request)
        if request.url.path.endswith("/graphql"):
            if "repository" in json.loads(request.content)["query"]:
                return httpx.Response(200, json={"data": {"q0": {
                    "_graphub_type": "Repository", "_graphub_id": "R", "_graphub_fullName": "o/r",
                }}})
            return httpx.Response(200, json={"data": {"q0": {
                "_graphub_type": "CheckRun", "_graphub_id": "CR", "databaseId": "not-an-integer", "name": "job",
            }}})
        assert request.url.path.endswith("/contents/file")
        return httpx.Response(200, json={"name": "file", "type": "file", "size": 2**40})

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        for query, variables in [
            ('{repository(owner:"o",name:"r"){issue(number:2147483648){title}}}', {}),
            ('query($n:Int!){repository(owner:"o",name:"r"){issue(number:$n){title}}}', {"n": 2**40}),
        ]:
            invalid = await call(tools, query, variables=variables)
            assert invalid["errors"] and not seen
        invalid = await call(tools, '{node(id:"CR"){... on CheckRun{databaseId name}}}')
        assert invalid["data"]["node"] == {"databaseId": None, "name": "job"}
        assert invalid["errors"][0]["path"] == ["node", "databaseId"]
        local = await call(tools, '{repository(owner:"o",name:"r"){file(path:"file"){name size}}}')
        assert local["data"]["repository"]["file"] == {"name": "file", "size": None}
        assert local["errors"][0]["path"] == ["repository", "file", "size"]


@pytest.mark.asyncio
async def test_backend_errors_keep_successful_roots_and_graphql_null_propagation(tmp_path):
    async def server(request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "q0": {
                        "_graphub_type": "Repository",
                        "_graphub_id": "R",
                        "_graphub_fullName": "o/r",
                        "name": "r",
                        "issue": None,
                    },
                    "q1": {"login": "alice", "_graphub_type": "User", "_graphub_id": "U"},
                },
                "errors": [
                    {
                        "message": "Forbidden comment",
                        "path": ["q0", "issue", "title"],
                        "extensions": {"code": "FORBIDDEN"},
                    },
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        out = await call(
            GitHubDSLTool(client, RecordingStorage(tmp_path), token=""),
            '{r:repository(owner:"o",name:"r"){name issue(number:1){title}} viewer{login}}',
        )
        assert out["data"] == {"r": {"name": "r", "issue": None}, "viewer": {"login": "alice"}}
        assert (
            out["errors"][0]["path"] == ["r", "issue", "title"] and out["errors"][0]["message"] == "Forbidden comment"
        )


@pytest.mark.asyncio
async def test_nullable_node_items_retain_all_native_errors_and_successful_siblings(tmp_path):
    fixture = UnifiedServer()

    async def server(request):
        query = json.loads(request.content)["query"]
        root = {"nodes": [GraphQLError("missing first", extensions={"code": "NOT_FOUND"}),
                          fixture.issue(1), GraphQLError("missing third")], "repository": fixture.repo}
        result = graphql_sync(public_schema(), query, root_value=root).formatted
        result["errors"][1]["type"] = "NOT_FOUND"
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        out = await call(tools, '{bad:nodes(ids:["x","I1","y"]){...on Issue{number}} '
                               'ok:repository(owner:"o",name:"r"){name}}')
        assert out["data"] == {"bad": [None, {"number": 1}, None], "ok": {"name": "r"}}
        assert [e["path"] for e in out["errors"]] == [["bad", 0], ["bad", 2]]
        assert out["errors"][0]["extensions"]["code"] == "NOT_FOUND"
        assert out["errors"][1]["extensions"]["type"] == "NOT_FOUND"
        saved = json.loads(tools.api._body(tools.api._saved(out["extensions"]["result_id"])))
        assert saved["errors"] == out["errors"]


@pytest.mark.asyncio
async def test_multiple_errors_below_a_null_ancestor_are_not_reduced_to_the_first(tmp_path):
    async def server(_):
        return httpx.Response(200, json={"data": {"q0": None}, "errors": [
            {"message": "name failed", "path": ["q0", "name"]},
            {"message": "id failed", "path": ["q0", "id"]},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        out = await call(GitHubDSLTool(client, RecordingStorage(tmp_path), token=""),
                         '{r:repository(owner:"o",name:"r"){name id}}')
        assert out["data"] == {"r": None}
        assert [e["path"] for e in out["errors"]] == [["r", "name"], ["r", "id"]]


@pytest.mark.asyncio
async def test_graphql_alias_namespace_is_not_reserved(tmp_path):
    server = UnifiedServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        out = await call(
            GitHubDSLTool(client, RecordingStorage(tmp_path), token=""),
            '{_graphub_type:repository(owner:"o",name:"r"){_graphub_id:name _dsl_x:id}}',
        )
        assert out["data"] == {"_graphub_type": {"_graphub_id": "r", "_dsl_x": "R"}}
        assert not out.get("errors")
