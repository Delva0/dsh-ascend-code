"""Read-only GraphQL, lossless local views, bounded pagination and CI log evidence."""

import base64
import json
import time

import httpx
import pytest

from ascend_github.core.tool_github import REST_DESCRIPTION, GitHubRESTTool

from .helpers import RecordingStorage


async def call(tools, request):
    return (await tools.github_api("test", [request]))["results"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    {"query": 'mutation { deleteIssue(input: {issueId: "x"}) {clientMutationId}}'},
    {"query": "query Safe {viewer {login}} mutation Write {anything}", "operation_name": "Safe"},
    {"query": "subscription {anything}"},
    {"query": "schema {query: Query}"},
    {"query": "fragment F on User {login}"},
    {"query": "query A {viewer {login}} query B {viewer {id}}"},
    {"query": "query A {viewer {login}}", "operation_name": "B"},
    {"query": "query {viewer {login}"},
    {"query": "{viewer {login}}", "path": "/graphql"},
    {"query": "{viewer {login}}", "method": "GET"},
    {"path": "/graphql", "method": "POST"},
    {"path": "/graphql", "params": {"query": "mutation {anything}"}},
    {"path": "/%67raphql/", "params": {"query": "mutation {anything}"}},
    {"path": "/user", "variables": {}},
])
async def test_reject_non_query_documents_and_ambiguous_modes_before_http(tmp_path, case):
    async def fetch(_):
        pytest.fail("Rejected input must not reach HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        result = await call(GitHubRESTTool(client, RecordingStorage(tmp_path), token=""), case)
    assert "error" in result and "status" not in result


@pytest.mark.asyncio
async def test_graphql_document_variables_partial_errors_and_recovery(tmp_path):
    seen = []
    data = {"data": {"viewer": {"login": "reader"}, "repository": None},
            "errors": [{"message": "Forbidden field", "path": ["repository"], "extensions": None}]}
    query = "# mutation in comments is harmless\nquery Q($s:String!) {search(query:$s,type:ISSUE,first:1) {issueCount}}"

    async def fetch(request):
        seen.append(request)
        assert request.method == "POST" and request.url.path == "/graphql"
        assert request.headers["authorization"] == "Bearer secret"
        assert json.loads(request.content) == {"query": query, "variables": {"s": '"mutation"'}, "operationName": "Q"}
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="secret")
        result = await call(tools, {"query": query, "variables": {"s": '"mutation"'}, "operation_name": "Q"})
        selected = await call(tools, {"result_id": result["result_id"], "json_pointer": "/data/viewer", "view": "full"})
        raw = await call(tools, {"result_id": result["result_id"], "view": "raw"})
    assert len(seen) == 1 and result["status"] == 200 and result["partial_data"]
    assert result["error"]["type"] == "GitHubGraphQLError"
    assert selected["data"] == {"login": "reader"} and selected["graphql_errors"] == data["errors"]
    assert json.loads(raw["content"]) == data
    saved = json.loads(next(tmp_path.rglob("*.request.json")).read_text())
    assert saved["json"]["query"] == query and "authorization" not in saved["headers"]


@pytest.mark.asyncio
async def test_graphql_alias_fragment_and_redirect_boundary(tmp_path):
    seen = []

    async def fetch(request):
        seen.append(request)
        return httpx.Response(307, headers={"location": "/repos/example/repo"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        result = await call(GitHubRESTTool(client, RecordingStorage(tmp_path), token=""), {
            "query": "query {mutation:viewer {...F}} fragment F on User {login}",
        })
    assert len(seen) == 1 and result["status"] == 307
    assert "redirects are not followed" in result["error"]["message"]


@pytest.mark.asyncio
async def test_graphql_rate_limit_is_separate_from_rest(tmp_path):
    seen = []

    async def fetch(request):
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": None, "errors": [{"type": "RATE_LIMITED"}]},
                                  headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(time.time() + 1000)})
        return httpx.Response(200, json={"login": "reader"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        limited = await call(tools, {"query": "{viewer {login}}"})
        paused = await call(tools, {"query": "{viewer {id}}"})
        rest = await call(tools, {"path": "/user"})
    assert len(seen) == 2 and limited["rate_limited"] and not limited["partial_data"]
    assert paused["resource"] == "graphql" and rest["data"] == {"login": "reader"}


@pytest.mark.asyncio
async def test_semantic_query_is_documented_and_not_rewritten(tmp_path):
    seen = []
    params = {"q": 'repo:o/r is:issue "reset password" OR login', "search_type": "semantic"}

    async def fetch(request):
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        await call(
            GitHubRESTTool(client, RecordingStorage(tmp_path), token=""), {"path": "/search/issues", "params": params},
        )
    assert dict(seen[0].url.params) == params
    assert "search_type='semantic'" in REST_DESCRIPTION and "semantically_similar" in REST_DESCRIPTION


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [{}, {"json_pointer": "/items"}])
async def test_projection_preserves_missing_null_escaped_keys_and_original(tmp_path, selection):
    original = {"items": [{"number": 1, "user": {"login": "a"}, "a/b": {"~value": None}}, {"number": 2}]}

    async def fetch(_):
        return httpx.Response(200, json=original)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        result = await call(tools, {"path": "/search/issues", "params": {"q": "timeout"},
                                   **selection, "view": "full",
                                   "fields": {"n": "/number", "who": "/user/login", "nullable": "/a~1b/~0value"}})
        restored = await call(tools, {"result_id": result["result_id"], "view": "full"})
        invalid = await call(tools, {"result_id": result["result_id"], "json_pointer": "/items/0/number",
                                    "fields": {"x": "/x"}})
    assert result["data"] == [{"n": 1, "who": "a", "nullable": None}, {"n": 2}]
    assert result["omission_count"] == 3 and not result["display_complete"]
    assert restored["data"] == original and restored["display_complete"]
    assert invalid["result_id"] == result["result_id"] and "error" in invalid


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/search/issues", "https://api.github.com/search/pulls?q=repo:o/r"])
async def test_search_projection_recovery_root_override_and_continuation(tmp_path, path):
    seen = []
    items = [{"number": 1, "title": "First"}, {"number": 2, "title": "Second"}]
    original = {"total_count": 2, "incomplete_results": False, "items": items}
    fields = {"n": "/number", "title": "/title"}

    async def fetch(request):
        seen.append(request)
        return httpx.Response(200, json=original)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        result = await call(tools, {"path": path, "params": {"q": "repo:o/r timeout"},
                                   "fields": fields, "max_chars": 25})
        assert result["json_pointer"] == "/items"
        saved = result["result_id"]
        chunks = [result["content"]]
        while result["continue_request"]:
            assert result["continue_request"]["json_pointer"] == "/items"
            result = await call(tools, result["continue_request"])
            chunks.append(result["content"])
        restored = await call(tools, {"result_id": saved, "fields": fields})
        root = await call(tools, {"result_id": saved, "json_pointer": "",
                                 "fields": {"count": "/total_count", "incomplete": "/incomplete_results"}})
        one = await call(tools, {"result_id": saved, "json_pointer": "/items/0", "fields": fields})
        whole = await call(tools, {"result_id": saved, "view": "full"})
    expected = [{"n": 1, "title": "First"}, {"n": 2, "title": "Second"}]
    assert json.loads("".join(chunks)) == restored["data"] == expected
    assert restored["json_pointer"] == "/items" and restored["from_result_id"]
    assert root["data"] == {"count": 2, "incomplete": False} and root["json_pointer"] == ""
    assert one["data"] == expected[0]
    assert whole["data"] == original and whole["json_pointer"] == ""
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_search_collection_projects_items_and_recovers_without_http(tmp_path):
    seen = []

    async def fetch(request):
        seen.append(request)
        headers = ({"link": '<https://api.github.com/search/issues?q=timeout&page=2>; rel="next"'}
                   if len(seen) == 1 else {})
        return httpx.Response(200, json={"items": [{"number": len(seen)}]}, headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        result = await call(tools, {"path": "/search/issues", "params": {"q": "timeout"},
                                   "fields": {"n": "/number"},
                                   "paginate": {"items_pointer": "/items", "max_pages": 2, "max_items": 10}})
        restored = await call(tools, {"result_id": result["result_id"], "fields": {"n": "/number"}})
    assert result["data"] == restored["data"] == [{"n": 1}, {"n": 2}]
    assert result["json_pointer"] == restored["json_pointer"] == "/items"
    assert result["collection"]["complete"] and len(seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "status", "body", "fields", "expected", "pointer"), [
    ("/search/issues", 200, {"total_count": 0, "items": []}, {"n": "/number"}, [], "/items"),
    ("/search/issues", 422, {"message": "Validation Failed"}, {"message": "/message"},
     {"message": "Validation Failed"}, ""),
    ("/search/issues", 200, {"items": None}, {"items": "/items"}, {"items": None}, ""),
    ("/repos/o/r/issues/1", 200, {"number": 1, "items": [{"number": 2}]}, {"n": "/number"}, {"n": 1}, ""),
])
async def test_search_projection_empty_hits_errors_and_non_search_objects(tmp_path, path, status, body, fields,
                                                                         expected, pointer):
    async def fetch(_):
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        result = await call(GitHubRESTTool(client, RecordingStorage(tmp_path), token=""), {
            "path": path, "params": {"q": "repo:o/r timeout"}, "fields": fields,
        })
    assert result["status"] == status and result["data"] == expected and result["json_pointer"] == pointer
    if status == 422:
        assert result["error"]["code"] == "invalid_request"
    else:
        assert "error" not in result


@pytest.mark.asyncio
async def test_numbered_file_find_patch_and_long_line_continuation(tmp_path):
    source = "first\n" + "long " * 100 + "\nneedle\nlast\n"

    async def fetch(_):
        return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(source.encode()).decode(),
                                       "patch": "@@ -1 +1 @@\n-old\n+new\n"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        request = {"path": "/repos/o/r/contents/a", "view": "text", "start_line": 2, "end_line": 2, "max_chars": 70}
        result = await call(tools, request)
        saved = result["result_id"]
        chunks = [result["content"]]
        while result["next_offset"] is not None:
            assert result["next_line"] is None
            result = await call(tools, {k: v for k, v in request.items() if k != "path"}
                                | {"result_id": saved, "offset": result["next_offset"]})
            chunks.append(result["content"])
        found = await call(tools, {"result_id": saved, "view": "text", "find": "needle", "context_lines": 0})
        patch = await call(tools, {"result_id": saved, "view": "full", "json_pointer": "/patch", "tail_lines": 2})
        missing = await call(tools, {"result_id": saved, "view": "text", "find": "absent", "context_lines": 1})
    assert "".join(chunks) == "2: " + "long " * 100 + "\n" and result["next_line"] == 3
    assert found["content"] == "3: needle\n" and found["match_line"] == 3
    assert patch["content"] == "2: -old\n3: +new\n" and patch["total_lines"] == 3
    assert missing["match_line"] is None and missing["content"] == ""


@pytest.mark.asyncio
async def test_rest_collection_reuses_unconsumed_saved_page(tmp_path):
    seen = []

    async def fetch(request):
        seen.append(str(request.url))
        page = int(request.url.params.get("page", 1))
        headers = {"link": f'<https://api.github.com/issues?page={page + 1}>; rel="next"'} if page < 3 else {}
        return httpx.Response(200, json={"items": [{"id": n} for n in range(page * 3 - 2, page * 3 + 1)]},
                              headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, {"path": "/issues", "view": "full",
                                  "paginate": {"items_pointer": "/items", "max_pages": 3, "max_items": 4}})
        next_request = first["collection"]["next_request"]
        assert len(seen) == 2 and next_request["paginate"]["start_index"] == 1
        next_request["paginate"]["max_items"] = 10
        rest = await call(tools, next_request)
        restored = await call(tools, {"result_id": first["result_id"], "view": "full"})
    assert [i["id"] for i in first["data"]["items"] + rest["data"]["items"]] == list(range(1, 10))
    assert len(seen) == 3 and rest["collection"]["complete"] and rest["collection"]["next_request"] is None
    assert not first["collection"]["complete"] and first["collection"]["stop_reason"] == "max_items"
    assert rest["collection"]["pages"][0]["from_result_id"] and restored["data"] == first["data"]
    assert "status" not in first  # A derived collection is not a synthetic HTTP 200.


@pytest.mark.asyncio
async def test_graphql_collection_changes_only_named_cursor(tmp_path):
    seen = []
    query = ('query Q($cursor:String,$label:String!) {repository(owner:"o",name:"r") '
             '{issues(first:2,after:$cursor,labels:[$label]) {nodes {number} pageInfo {hasNextPage endCursor}}}}')

    async def fetch(request):
        body = json.loads(request.content)
        seen.append(body)
        last = body["variables"].get("cursor") == "next"
        return httpx.Response(200, json={"data": {"repository": {"issues": {
            "nodes": [{"number": 3 if last else 1}], "pageInfo": {"hasNextPage": not last, "endCursor": "next"},
        }}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        result = await call(GitHubRESTTool(client, RecordingStorage(tmp_path), token=""), {
            "query": query, "variables": {"label": "bug"}, "operation_name": "Q", "view": "full",
            "paginate": {"items_pointer": "/data/repository/issues/nodes",
                         "connection_pointer": "/data/repository/issues",
                         "cursor_variable": "cursor", "max_pages": 3, "max_items": 20},
        })
    assert result["collection"]["complete"] and result["data"]["items"] == [{"number": 1}, {"number": 3}]
    assert len(seen) == 2 and seen[1] == {**seen[0], "variables": {"label": "bug", "cursor": "next"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "http", "loop", "incomplete"])
async def test_collection_stops_without_losing_pages_or_claiming_completeness(tmp_path, failure):
    seen = []

    async def fetch(request):
        seen.append(request)
        if len(seen) >= 2:
            if failure == "network":
                raise httpx.ConnectError("offline")
            return httpx.Response(503, json={"message": "unavailable"})
        headers = {} if failure == "incomplete" else {
            "link": f'<https://api.github.com/issues{"" if failure == "loop" else "?page=2"}>; rel="next"',
        }
        return httpx.Response(200, json={"items": [{"id": 1}], "incomplete_results": failure == "incomplete"},
                              headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        result = await call(GitHubRESTTool(client, RecordingStorage(tmp_path), token=""), {
            "path": "/issues", "paginate": {"items_pointer": "/items", "max_pages": 3, "max_items": 10},
        })
    assert not result["collection"]["complete"] and result["data"]["items"] == [{"id": 1}]
    assert result["collection"]["stop_reason"] == ("source_incomplete" if failure == "incomplete" else "page_error")
    assert len(seen) == (1 if failure in {"loop", "incomplete"} else 3)


@pytest.mark.asyncio
async def test_ci_logs_bound_jobs_follow_without_credentials_and_resume(tmp_path):
    seen = []
    source = "start\n" + "error " * 40 + "\nend\n"

    async def fetch(request):
        seen.append(request)
        if request.url.host == "logs.example":
            assert not any(key in request.headers for key in ("authorization", "cookie", "x-client-secret"))
            assert request.headers["host"] == "logs.example"
            return httpx.Response(200, content=source.encode(), headers={"content-type": "application/octet-stream"})
        assert request.headers["authorization"] == "Bearer gh-secret"
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [{"id": 1, "conclusion": "success"},
                {"id": 2, "name": "linux", "conclusion": "failure"}, {"id": 3, "conclusion": "timed_out"}]})
        if request.url.path.endswith("/3/logs"):
            return httpx.Response(404, json={"message": "expired log"})
        return httpx.Response(302, headers={"location": "https://logs.example/signed?sig=value"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch), headers={"x-client-secret": "client-secret"},
                                cookies={"private": "cookie-secret"}, auth=("user", "pass")) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="gh-secret")
        first = await call(tools, {"ci_logs": {"owner": "o", "repo": "r", "run_id": 9, "max_jobs": 1,
                                             "tail_lines": 2, "max_chars_per_job": 30}, "view": "full"})
        log = first["data"]["logs"][0]
        continued = await call(tools, log["continue_request"])
        second = await call(tools, first["data"]["resume_request"] | {"view": "full"})
        raw = await call(tools, {"result_id": log["result_id"], "view": "raw"})
    assert len(seen) == 4 and first["ci"]["remaining_job_ids"] == [3] and not first["ci"]["complete"]
    assert log["id"] == 2 and log["start_line"] == 2 and continued["offset"] == 30
    assert base64.b64decode(raw["content"]) == source.encode()
    assert second["data"]["logs"][0]["error"]["type"] == "GitHubHTTPError"
    assert not second["ci"]["complete"]
    for path in tmp_path.rglob("*.request.json"):
        assert "gh-secret" not in path.read_text() and "cookie-secret" not in path.read_text()


@pytest.mark.asyncio
async def test_oversized_ci_log_retains_partial_body_and_continues_other_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr("ascend_github.core.github_api.MAX_LOG_BYTES", 8)

    async def fetch(request):
        if request.url.host == "logs.example":
            return httpx.Response(200, content=b"0123456789abcdef", headers={"content-type": "text/plain"})
        if request.url.path.endswith("/1/logs"):
            return httpx.Response(302, headers={"location": "https://logs.example/large"})
        return httpx.Response(200, text="small\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        result = await call(tools, {"ci_logs": {"owner": "o", "repo": "r", "run_id": 9, "job_ids": [1, 2]},
                                   "view": "full"})
        first, second = result["data"]["logs"]
        partial = await call(tools, {"result_id": first["result_id"], "view": "raw"})
    assert "partial body retained" in first["error"]["message"]
    assert partial["content"] == "01234567" and not partial["download_complete"]
    assert second["content"] == "1: small\n" and not result["ci"]["complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["partial", "no_data", "missing_cursor"])
async def test_graphql_collection_retains_evidence_and_provides_fresh_retry(tmp_path, failure):
    seen = []
    query = ("query Q($cursor:String) {viewer {repositories(first:2,after:$cursor) {nodes {id} "
             "pageInfo {hasNextPage endCursor}}}}")

    async def fetch(request):
        seen.append(request)
        page = {"data": {"viewer": {"repositories": {
            "nodes": [{"id": "r1"}], "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}}
        if len(seen) == 1:
            if failure == "missing_cursor":
                page["data"]["viewer"]["repositories"]["pageInfo"]["hasNextPage"] = True
            else:
                page["errors"] = [{"type": "FORBIDDEN", "message": "Missing field access"}]
                if failure == "no_data":
                    page["data"] = None
        return httpx.Response(200, json=page)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, {"query": query, "paginate": {
            "items_pointer": "/data/viewer/repositories/nodes", "connection_pointer": "/data/viewer/repositories",
            "cursor_variable": "cursor", "max_pages": 2, "max_items": 10,
        }})
        retry = await call(tools, first["collection"]["retry_request"])
        original = await call(tools, {"result_id": first["collection"]["pages"][0]["result_id"], "view": "full"})
    assert first["collection"]["stop_reason"] == "page_error" and first["collection"]["next_request"] is None
    assert first["data"]["items"] == ([] if failure == "no_data" else [{"id": "r1"}])
    if failure != "missing_cursor":
        assert first["error"]["type"] == "GitHubGraphQLError" and original["graphql_errors"]
    assert len(seen) == 2 and retry["collection"]["complete"] and retry["data"]["items"] == [{"id": "r1"}]


@pytest.mark.asyncio
async def test_ci_scan_budget_and_nonfinal_download_do_not_claim_completion(tmp_path):
    async def fetch(request):
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": []}, headers={
                "link": '<https://api.github.com/repos/o/r/actions/runs/9/jobs?page=2>; rel="next"',
            })
        if request.url.host == "logs.example":
            return httpx.Response(304)
        return httpx.Response(302, headers={"location": "https://logs.example/not-final"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        scan = await call(tools, {"ci_logs": {"owner": "o", "repo": "r", "run_id": 9, "max_pages": 1}})
        download = await call(tools, {"ci_logs": {"owner": "o", "repo": "r", "run_id": 9, "job_ids": [1]}})
    assert not scan["ci"]["complete"] and scan["ci"]["next_jobs_request"]
    assert scan["ci"]["scan_stop_reason"] == "max_pages" and scan["ci"]["logs_attempted"] == 0
    assert not download["ci"]["complete"] and download["ci"]["scope"] == "job_ids"
    assert "got 304" in download["data"]["logs"][0]["error"]["message"]
