"""Exercise source reading through defaults, PR file evidence and GitHub code links."""

import base64
import json

import httpx
import pytest

from ascend_github.core.github_api import GitHubResponseContent
from ascend_github.core.registry import ToolRegistry
from ascend_github.core.tool_github import GitHubDSLTool, GitHubRESTTool

from .helpers import RecordingStorage

SHA = "a" * 40
OLD_BLOB = "b" * 40


async def call(tools, request):
    return (await tools.github_api("code", [request]))["results"][0]


def file_response(text, **fields):
    return httpx.Response(
        200,
        json={
            "type": "file",
            "encoding": "base64",
            "sha": SHA,
            "content": base64.b64encode(text.encode()).decode(),
            **fields,
        },
    )


@pytest.mark.asyncio
async def test_default_file_text_is_lossless_and_explicit_views_still_select_metadata(tmp_path):
    source = '{ "unicode": "源码", "literal": "do not reformat" }\n' * 40
    seen = []

    async def fetch(request):
        seen.append(request)
        return file_response(source)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, {"path": "/repos/o/r/contents/code.json", "params": {"ref": SHA}, "max_chars": 90})
        chunks, result = [first["content"]], first
        while result["continue_request"]:
            result = await call(tools, result["continue_request"])
            chunks.append(result["content"])
        saved = {"result_id": first["result_id"]}
        numbered = await call(tools, {**saved, "start_line": 3, "max_lines": 2})
        compact = await call(tools, {**saved, "view": "compact"})
        full = await call(tools, {**saved, "view": "full"})
        projected = await call(tools, {**saved, "fields": {"sha": "/sha"}})
        raw = await call(tools, {**saved, "view": "raw"})
    assert "".join(chunks) == source and first["view"] == "text" and len(seen) == 1
    assert first["continue_request"]["view"] == "text"
    assert numbered["start_line"] == 3 and numbered["content"].startswith('3: { "unicode"')
    assert compact["data"]["encoding"] == "base64" and compact["display_complete"]
    assert base64.b64decode(compact["data"]["content"]).decode() == source
    assert base64.b64decode(full["data"]["content"]).decode() == source
    assert projected["data"] == {"sha": SHA} and json.loads(raw["content"]) == full["data"]


@pytest.mark.asyncio
async def test_directory_errors_and_large_file_metadata_are_not_decoded(tmp_path):
    replies = [
        httpx.Response(200, json=[{"name": "a.py", "type": "file"}]),
        httpx.Response(404, json={"message": "Not Found"}),
        httpx.Response(200, json={"type": "file", "encoding": "none", "content": "", "size": 2000000}),
    ]

    async def fetch(_):
        return replies.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        directory = await call(tools, {"path": "/repos/o/r/contents"})
        missing = await call(tools, {"path": "/repos/o/r/contents/missing"})
        large = await call(tools, {"path": "/repos/o/r/contents/large"})
    assert directory["data"] == [{"name": "a.py", "type": "file"}]
    assert missing["error"]["code"] == "not_found_or_inaccessible"
    assert large["data"]["encoding"] == "none" and large["view"] == "compact"


@pytest.mark.asyncio
async def test_long_patches_survive_default_projection_and_saved_continuations(tmp_path):
    patch = "@@ -1 +1 @@\n-old\n+new\n" * 80
    original = {"body": "b" * 1000, "patch": patch, "diff_hunk": patch}

    async def fetch(_):
        return httpx.Response(200, json=original)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        first = await call(tools, {"path": "/repos/o/r/pulls/comments/1"})
        projected = await call(tools, {"result_id": first["result_id"], "fields": {"code": "/diff_hunk"}})
        request = {"result_id": first["result_id"], "json_pointer": "/patch", "max_chars": 120}
        chunks = []
        while request:
            result = await call(tools, request)
            chunks.append(result["content"] if "content" in result else json.dumps(result["data"]))
            request = result["continue_request"]
    assert first["data"]["patch"] == first["data"]["diff_hunk"] == projected["data"]["code"] == patch
    assert first["data"]["body"] == original["body"]
    assert first["display_complete"] and not first["omissions"]
    assert json.loads("".join(chunks)) == patch


@pytest.mark.asyncio
async def test_pr_file_read_requests_follow_native_fork_revision_and_deleted_blob(tmp_path):
    native_url = f"https://api.github.com/repos/fork/r/contents/new.py?ref={SHA}"
    original = [
        {"filename": "new.py", "previous_filename": "old.py", "status": "renamed", "contents_url": native_url},
        {
            "filename": "gone.py",
            "status": "removed",
            "sha": OLD_BLOB,
            "contents_url": f"https://api.github.com/repos/o/r/contents/gone.py?ref={SHA}",
        },
    ]
    seen = []

    async def fetch(request):
        seen.append(request)
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=original)
        if "/git/blobs/" in request.url.path:
            assert request.url.path == f"/repos/o/r/git/blobs/{OLD_BLOB}"
            return file_response("old code\n")
        assert str(request.url) == native_url
        return file_response("new code\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        files = await call(tools, {"path": "/repos/o/r/pulls/1/files"})
        after = await call(tools, files["data"][0]["read_request"])
        before = await call(tools, files["data"][1]["read_request"])
        native = await call(tools, {"result_id": files["result_id"], "view": "full"})
        selected = await call(tools, {"result_id": files["result_id"], "fields": {"read": "/read_request"}})
    assert after["content"] == "new code\n" and before["content"] == "old code\n"
    assert native["data"] == original and len(seen) == 3
    assert selected["data"][0]["read"] == files["data"][0]["read_request"]
    assert json.loads((tmp_path / native["body_file"]).read_bytes()) == original


@pytest.mark.asyncio
async def test_pr_paginated_and_saved_views_keep_executable_requests_with_long_paths(tmp_path):
    filename = "/".join(["directory" * 10] * 8) + "/code.py"
    native_url = f"https://api.github.com/repos/o/r/contents/{filename}?ref={SHA}"

    async def fetch(request):
        if "/contents/" in request.url.path:
            assert str(request.url) == native_url
            return file_response("code\n")
        return httpx.Response(200, json=[{"filename": filename, "contents_url": native_url}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        files = await call(
            tools,
            {
                "path": "/repos/o/r/pulls/1/files",
                "paginate": {"items_pointer": "", "max_pages": 1, "max_items": 10},
                "max_chars": 100,
            },
        )
        restored = await call(
            tools, {"result_id": files["result_id"], "json_pointer": "/items", "fields": {"read": "/read_request"}},
        )
        source = await call(tools, restored["data"][0]["read"])
    assert source["content"] == "code\n"
    assert restored["data"][0]["read"]["path"] == native_url


@pytest.mark.asyncio
@pytest.mark.parametrize("host_path", ["github.com/o/r/blob", "github.com/o/r/raw", "raw.githubusercontent.com/o/r"])
async def test_commit_code_links_decode_paths_and_line_anchors_without_web_requests(tmp_path, host_path):
    seen = []

    async def fetch(request):
        seen.append(request)
        assert request.url.host == "api.github.com" and request.headers["authorization"] == "Bearer test"
        assert request.url.path == "/repos/o/r/contents/src/源 file#?.py"
        assert request.url.params["ref"] == SHA
        return file_response("first\nsecond\nthird\nfourth\n")

    url = f"https://{host_path}/{SHA}/src/%E6%BA%90%20file%23%3F.py?plain=1#L2-L3"
    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        registry = ToolRegistry((GitHubRESTTool(client, RecordingStorage(tmp_path), token="test"), "github"))
        result = (await registry.call("github", "link", {"requests": [{"path": url}]}))["results"][0]
    assert result["content"] == "2: second\n3: third\n" and result["continue_request"] is None
    assert len(seen) == 1 and result["path"] == url and result["view"] == "text"


@pytest.mark.asyncio
async def test_named_link_resolves_longest_ref_and_pins_branch_snapshot(tmp_path):
    seen = []

    async def fetch(request):
        seen.append(request)
        if "/matching-refs/heads/" in request.url.path:
            return httpx.Response(
                200,
                json=[
                    {"ref": "refs/heads/release", "object": {"type": "commit", "sha": OLD_BLOB}},
                    {"ref": "refs/heads/release/1.0", "object": {"type": "commit", "sha": SHA}},
                ],
            )
        if "/matching-refs/tags/" in request.url.path:
            return httpx.Response(200, json=[])
        assert request.url.path == "/repos/o/r/contents/src/a.py" and request.url.params["ref"] == SHA
        return file_response("one\ntwo\nthree\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="")
        result = await call(
            tools, {"path": "https://github.com/o/r/blob/release/1.0/src/a.py#L2", "start_line": 1, "max_lines": 1},
        )
        following = await call(tools, result["continue_request"])
    assert result["content"] == "1: one\n" and following["content"] == "2: two\n"
    assert len(seen) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "params", "expected_ref", "lookups"),
    [
        ("https://github.com/o/r/blob/v1/src/a.py", {}, "refs/tags/v1", 2),
        ("https://github.com/o/r/blob/release%2F1.0/src/a.py", {}, "release/1.0", 0),
        ("https://github.com/o/r/blob/release/1.0/src/a.py", {"ref": "release/1.0"}, "release/1.0", 0),
        ("https://raw.githubusercontent.com/o/r/refs/tags/v1/src/a.py", {}, "refs/tags/v1", 1),
        ("https://raw.githubusercontent.com/o/r/refs/heads/release%2F1.0/src/a.py", {}, "refs/heads/release/1.0", 0),
    ],
)
async def test_tags_encoded_slashes_and_explicit_refs(tmp_path, url, params, expected_ref, lookups):
    seen = []

    async def fetch(request):
        seen.append(request)
        if "/matching-refs/" in request.url.path:
            return httpx.Response(
                200,
                json=[{"ref": "refs/tags/v1", "object": {"type": "tag", "sha": OLD_BLOB}}]
                if "/tags/" in request.url.path
                else [],
            )
        assert request.url.params["ref"] == expected_ref and request.url.path == "/repos/o/r/contents/src/a.py"
        return file_response("code\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        result = await call(
            GitHubRESTTool(client, RecordingStorage(tmp_path), token=""), {"path": url, "params": params},
        )
    assert result["content"] == "code\n" and len(seen) == lookups + 1


@pytest.mark.asyncio
async def test_code_link_errors_do_not_guess_revisions_or_expand_credential_origins(tmp_path):
    seen = []

    async def fetch(request):
        seen.append(request)
        assert request.url.host == "api.github.com"
        return httpx.Response(403, json={"message": "Unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubRESTTool(client, RecordingStorage(tmp_path), token="test")
        for url in [
            "http://github.com/o/r/blob/main/a.py",
            "https://secret@github.com/o/r/blob/main/a.py",
            "https://github.com:444/o/r/blob/main/a.py",
            "https://github.com/o/r/issues/1",
            f"https://github.com/o/r/blob/{SHA}/a.py#L3-L1",
            f"https://github.com/o/r/blob/{SHA}/a.py#unrecognized",
            f"https://github.com/o/r/blob/{SHA}/%2E%2E/secret",
        ]:
            assert "error" in await call(tools, {"path": url})
        assert not seen
        failed = await call(tools, {"path": "https://github.com/o/r/blob/main/a.py"})
        original = await call(tools, {"result_id": failed["result_id"], "view": "full"})
    assert len(seen) == 1 and failed["status"] == 403 and original["data"]["message"] == "Unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_missing_and_ambiguous_named_refs_never_read_a_guessed_file(tmp_path, ambiguous):
    seen = []

    async def fetch(request):
        seen.append(request)
        assert "/git/matching-refs/" in request.url.path
        namespace = "heads" if "/heads/" in request.url.path else "tags"
        return httpx.Response(
            200,
            json=[{"ref": f"refs/{namespace}/main", "object": {"type": "commit", "sha": SHA}}] if ambiguous else [],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        result = await call(
            GitHubRESTTool(client, RecordingStorage(tmp_path), token=""), {"path": "https://github.com/o/r/blob/main/a.py"},
        )
    assert "error" in result and len(seen) == 2


async def dsl_call(tools, query, **options):
    result = await ToolRegistry((tools, "github")).call("github", "code", {"query": query, **options})
    assert not result.get("errors"), result
    return result


async def display_text(tools, result, text):
    chunks = [text]
    while result.get("extensions", {}).get("display"):
        windows = result["extensions"]["display"][0]["windows"]
        assert len(windows) == 1
        result = await tools.github_dsl("next", **windows[0]["continue_request"])
        assert not result.get("errors"), result
        chunks.append(result["data"]["saved"])
    return "".join(chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["get", "code"])
async def test_dsl_code_link_line_range_survives_selection_budget_and_saved_reads(tmp_path, alias):
    source = "first\nsecond line has a long body\nthird\nfourth\n"
    seen = []

    async def fetch(request):
        seen.append(request)
        assert request.url.path == "/repos/o/r/contents/a.py" and request.url.params["ref"] == SHA
        return file_response(source)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        url = f"https://github.com/o/r/blob/{SHA}/a.py#L2-L3"
        result = await dsl_call(
            tools, "query($url:String!){" + alias + ":get(request:{path:$url})}", variables={"url": url}, max_chars=12,
        )
        assert await display_text(tools, result, result["data"][alias]) == "2: second line has a long body\n3: third\n"
        assert result["extensions"]["reads"][0]["total_lines"] == 4 and len(seen) == 1
        override = await dsl_call(
            tools, "query($url:String!){get(request:{path:$url,start_line:4,max_lines:1})}", variables={"url": url},
        )
        assert override["data"]["get"] == "4: fourth\n" and len(seen) == 1
        saved = await dsl_call(
            tools,
            "query($id:ID!){get(request:{result_id:$id})}",
            variables={"id": result["extensions"]["source_result_ids"][0]},
        )
        assert saved["data"]["get"] == source and len(seen) == 1


@pytest.mark.asyncio
async def test_source_decoding_is_lazy_and_once_per_read_across_rest_and_dsl(tmp_path, monkeypatch):
    source = "source line\n" * 10000
    decodes, seen = [], []
    decode = GitHubResponseContent.decode_file

    def decoded(data):
        decodes.append(data["sha"])
        return decode(data)

    monkeypatch.setattr(GitHubResponseContent, "decode_file", staticmethod(decoded))

    async def fetch(request):
        seen.append(request)
        return file_response(source, name="a.py")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        metadata = await dsl_call(tools, '{get(request:{path:"/repos/o/r/contents/a.py",fields:{name:"/name"}})}')
        assert metadata["data"]["get"] == {"name": "a.py"} and not decodes
        result = await dsl_call(tools, '{get(request:{path:"/repos/o/r/contents/a.py"})}', max_chars=80)
        assert source.startswith(result["data"]["get"]) and len(decodes) == 1 and len(seen) == 1
        assert await display_text(tools, result, result["data"]["get"]) == source
        full = await dsl_call(tools, '{get(request:{path:"/repos/o/r/contents/a.py",view:"full"})}', max_chars=40000)
        assert full["data"]["get"]["encoding"] == "base64" and len(decodes) == 1
        rest = await call(
            GitHubRESTTool(client, tools.storage, token=""), {"path": "/repos/o/r/contents/a.py", "max_chars": 80},
        )
        assert rest["content"] == result["data"]["get"] and len(decodes) == 2


@pytest.mark.asyncio
async def test_dsl_pr_read_targets_are_selectable_executable_and_preserve_original(tmp_path):
    native_url = f"https://api.github.com/repos/fork/r/contents/new.py?ref={SHA}"
    original = [
        {"filename": "new.py", "status": "renamed", "contents_url": native_url, "patch": "+a long patch\n" * 150},
        {"filename": "gone.py", "status": "removed", "sha": OLD_BLOB},
    ]
    seen = []

    async def fetch(request):
        seen.append(request)
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=original)
        assert str(request.url) == native_url or request.url.path == f"/repos/o/r/git/blobs/{OLD_BLOB}"
        return file_response("deleted\n" if "/git/blobs/" in request.url.path else "fork source\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        default = await dsl_call(tools, '{get(request:{path:"/repos/o/r/pulls/1/files"})}', max_chars=800)
        assert default["data"]["get"][0]["read_request"] == {"path": native_url}
        selected = await dsl_call(
            tools,
            '{get(request:{path:"/repos/o/r/pulls/1/files",fields:{filename:"/filename",read_request:"/read_request"}})}',
        )
        assert len(selected["data"]["get"]) == 2
        for item, text in zip(selected["data"]["get"], ["fork source\n", "deleted\n"], strict=True):
            result = await dsl_call(
                tools, "query($read:JSON!){get(request:$read)}", variables={"read": item["read_request"]},
            )
            assert result["data"]["get"] == text
        saved_id = selected["extensions"]["source_result_ids"][0]
        saved = await dsl_call(
            tools, 'query($id:ID!){saved(id:$id,fields:["filename","read_request"])}', variables={"id": saved_id},
        )
        assert saved["data"]["saved"] == selected["data"]["get"]
        raw = await dsl_call(
            tools, 'query($id:ID!){get(request:{result_id:$id,view:"raw"})}', variables={"id": saved_id},
        )
        full = await dsl_call(
            tools, 'query($id:ID!){get(request:{result_id:$id,view:"full"})}', variables={"id": saved_id},
        )
        assert json.loads(raw["data"]["get"]) == full["data"]["get"] == original and len(seen) == 3


@pytest.mark.asyncio
async def test_dsl_complete_patch_selection_and_continuations_do_not_apply_an_intermediate_budget(tmp_path):
    patch = "@@ -1 +1 @@\n-old\n+new\n" * 90
    seen = []

    async def fetch(request):
        seen.append(request)
        return httpx.Response(200, json={"body": "irrelevant " * 5000, "diff_hunk": patch})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        tools = GitHubDSLTool(client, RecordingStorage(tmp_path), token="")
        query = '{get(request:{path:"/repos/o/r/pulls/comments/1",fields:{diff_hunk:"/diff_hunk"}})}'
        result = await dsl_call(tools, query, max_chars=80)
        assert await display_text(tools, result, result["data"]["get"]["diff_hunk"]) == patch and len(seen) == 1
        full = await dsl_call(tools, query)
        assert full["data"]["get"]["diff_hunk"] == patch and not full["extensions"].get("display")
