"""Search uses native schema types and keeps explicit search scope unchanged."""

import json

import httpx
import pytest
from graphql import graphql_sync

from ascend_github.core.tool_github import GitHubDSLTool, public_schema

from .helpers import RecordingStorage, call


class SearchServer:
    def __init__(self, kind="PullRequest"):
        self.kind, self.queries, self.requests = kind, [], []

    async def __call__(self, request):
        self.requests.append(request)

        def search(info, **args):
            self.queries.append(args)
            number = 2 if args.get("after") else 1
            return {
                "nodes": [
                    {
                        "__typename": self.kind,
                        "id": f"N{number}",
                        "number": number,
                        "title": f"fix {number}",
                        "mergedAt": None,
                        "repository": {"nameWithOwner": "o/r"},
                    },
                ],
                "issueCount": 2,
                "issueSearchType": "SEMANTIC" if args["type"] == "ISSUE_SEMANTIC" else "LEXICAL",
                "lexicalFallbackReason": None,
                "pageInfo": {
                    "hasNextPage": number == 1,
                    "hasPreviousPage": False,
                    "startCursor": str(number),
                    "endCursor": str(number),
                },
            }

        result = graphql_sync(public_schema(), json.loads(request.content)["query"], root_value={"search": search})
        assert not result.errors, result.errors
        return httpx.Response(200, json=result.formatted)


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "mode"), [("Issue", "ISSUE"), ("PullRequest", "ISSUE"), ("Issue", "ISSUE_SEMANTIC")])
async def test_native_search_types_scopes_and_counts(tmp_path, kind, mode):
    server = SearchServer(kind)
    text = "repo:o/r created:>=2026-09-23 " + ("is:pr" if kind == "PullRequest" else "is:issue") + " incorrect outputs"
    query = (
        "query($q:String!){search(query:$q,type:"
        + mode
        + ",first:1){issueCount issueSearchType lexicalFallbackReason nodes{... on "
        + kind
        + "{number title}} pageInfo{endCursor hasNextPage}}}"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        out = await call(GitHubDSLTool(client, RecordingStorage(tmp_path), token=""), query, variables={"q": text})
    assert not out.get("errors"), out
    assert server.queries[0]["query"] == text and server.queries[0]["type"] == mode
    assert out["data"]["search"]["issueCount"] == 2
    assert out["data"]["search"]["nodes"] == [{"number": 1, "title": "fix 1"}]
    assert out["data"]["search"]["issueSearchType"] == ("SEMANTIC" if mode == "ISSUE_SEMANTIC" else "LEXICAL")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        '{search(query:"x"){nodes{__typename}}}',
        '{search(query:"x",type:"ISSUE"){issueCount}}',
        "{search(query:x,type:ISSUE){issueCount}}",
        '{issues(q:"repo:o/r is:pr"){number mergedAt}}',
        '{search(query:"x",type:CODE){issueCount}}',
    ],
)
async def test_old_inference_and_coercion_rules_are_gone(tmp_path, query):
    server = SearchServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        out = await call(GitHubDSLTool(client, RecordingStorage(tmp_path), token=""), query)
    assert out["errors"] and not server.requests
