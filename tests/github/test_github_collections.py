"""Field selection keeps the same item semantics when GitHub results are paginated."""

import json

import httpx
import pytest

from ascend_github.core.storage import ToolStorage
from ascend_github.core.tool_github import GitHubGraphQLTool, GitHubRESTTool


@pytest.mark.asyncio
@pytest.mark.parametrize("graphql", [False, True])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("view", ["compact", "full"])
async def test_collection_fields_select_items_and_preserve_saved_root(tmp_path, graphql, empty, view):
    items = [] if empty else [{"body": "Original comment", "user": {"login": "author"}}]
    fields = {"comment": "/body", "author": "/user/login"}
    reads = []

    def source(request):
        reads.append(request)
        data = items
        if graphql:
            data = {"data": {"repository": {"issues": {
                "nodes": items, "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(source)) as client:
        provider = GitHubGraphQLTool if graphql else GitHubRESTTool
        tool = provider(client, ToolStorage(tmp_path), token="")
        call = tool.github_graphql if graphql else tool.github_rest
        request = {"path": "/repos/o/r/pulls/1/comments"}
        pagination = {"items_pointer": "", "max_pages": 2, "max_items": 40}
        if graphql:
            request = {"query": 'query($cursor:String){repository(owner:"o",name:"r")'
                       '{issues(first:10,after:$cursor){nodes{body user:author{login}}'
                       'pageInfo{hasNextPage endCursor}}}}'}
            pagination.update(items_pointer="/data/repository/issues/nodes",
                              connection_pointer="/data/repository/issues", cursor_variable="cursor")
        first = (await call("collect", [{**request, "paginate": pagination, "fields": fields,
                                        "view": view, "max_chars": 25}]))["results"][0]
        assert first["json_pointer"] == "/items"
        assert first["collection"]["complete"] and first["collection"]["item_count"] == len(items)
        saved = first["result_id"]
        window, chunks = first, []
        while "content" in window:
            chunks.append(window["content"])
            continuation = window["continue_request"]
            if not continuation:
                break
            assert continuation["json_pointer"] == "/items"
            window = (await call("continue", [continuation]))["results"][0]
        expected = [{"comment": item["body"], "author": item["user"]["login"]} for item in items]
        assert (json.loads("".join(chunks)) if chunks else first["data"]) == expected
        selected, root, full = (await call("saved", [
            {"result_id": saved, "fields": fields, "view": view},
            {"result_id": saved, "json_pointer": "", "fields": {"comments": "/items"}, "view": "full"},
            {"result_id": saved, "view": "full"},
        ]))["results"]
        assert selected["data"] == expected and selected["from_result_id"]
        assert not any(item["reason"] == "missing_projection_field" for item in selected["omissions"])
        assert root["data"] == {"comments": items} and root["json_pointer"] == ""
        assert full["data"] == {"items": items} and full["json_pointer"] == ""
        if items:
            one = (await call("item", [{"result_id": saved, "json_pointer": "/items/0",
                                      "fields": fields}]))["results"][0]
            assert one["data"] == expected[0]
    assert len(reads) == 1
