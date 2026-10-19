"""REST and GraphQL mock servers shared by the DSL adapter regressions."""

import base64
import json
import math

import httpx
from graphql import graphql_sync

from ascend_github.core.tool_github import public_schema


def connection(items, *, following=False, previous=False, count=None):
    return {
        "nodes": items,
        "totalCount": len(items) if count is None else count,
        "issueCount": len(items) if count is None else count,
        "pageInfo": {"hasNextPage": following, "endCursor": "end", "hasPreviousPage": previous, "startCursor": "start"},
    }


class GitHubServer:
    """GraphQL execution validates aliases, fragments, field types and distinct per-parent cursors."""

    def __init__(self):
        self.requests, self.comments = [], []

    def issue(self, number):
        def comments(info, **args):
            self.comments.append((number, args))
            comment = {
                "__typename": "IssueComment",
                "id": f"C{number}",
                "body": f"comment {number}",
                "author": {"__typename": "User", "id": "U1", "login": "alice"},
            }
            return connection([comment], following=not args.get("after"), count=2)

        return {
            "__typename": "Issue",
            "id": f"I{number}",
            "number": number,
            "title": f"Issue {number}",
            "body": "long diagnostic body\n" * 1200,
            "comments": comments,
            "repository": {"__typename": "Repository", "id": "R1", "nameWithOwner": "o/r"},
        }

    async def __call__(self, request):
        self.requests.append(request)
        if request.method == "GET":
            page = request.url.params.get("page", "1")
            return httpx.Response(
                200,
                json={
                    "total_count": 2,
                    "incomplete_results": False,
                    "items": [{"number": int(page), "title": "relevant", "body": "long\n" * 5000, "comments": 2}],
                },
                headers={
                    "Link": '<https://api.github.com/search/issues?q=repo:o/r&page=2>; rel="next"',
                }
                if page == "1"
                else {},
            )
        payload = json.loads(request.content)
        root = {
            "repository": lambda info, **args: {
                "__typename": "Repository",
                "id": "R1",
                "name": "r",
                "nameWithOwner": "o/r",
                "issue": lambda info, number: self.issue(number),
                "object": lambda info, expression: {"__typename": "Blob", "id": "B1", "text": "file content"},
            },
            "search": lambda info, **args: connection([self.issue(1), self.issue(2)], following=True, count=4),
            "node": lambda info, **args: self.issue(int(args["id"][1:])),
        }
        result = graphql_sync(
            public_schema(), payload["query"], root_value=root, variable_values=payload.get("variables"),
        )
        assert not result.errors, result.errors
        return httpx.Response(200, json=result.formatted)


class UnifiedServer:
    def __init__(self, count=7):
        self.count, self.requests = count, []
        self.fail_after = None
        self.search_count = None

    def page(self, items, args):
        start, end = int(args.get("after", 0)), int(args.get("before", len(items)))
        if args.get("last") is not None:
            start = max(start, end - args["last"])
        else:
            end = min(end, start + args.get("first", 20))
        return {
            "nodes": items[start:end],
            "edges": [{"cursor": str(i), "node": item} for i, item in enumerate(items[start:end], start)],
            "totalCount": len(items),
            "issueCount": self.search_count or len(items),
            "repositoryCount": len(items),
            "userCount": len(items),
            "discussionCount": len(items),
            "issueSearchType": "LEXICAL",
            "lexicalFallbackReason": None,
            "pageInfo": {
                "hasNextPage": end < len(items),
                "hasPreviousPage": start > 0,
                "startCursor": str(start),
                "endCursor": str(end),
            },
        }

    def issue(self, number, kind="Issue"):
        return {
            "__typename": kind,
            "id": ("I" if kind == "Issue" else "P") + str(number),
            "number": number,
            "title": f"title {number}",
            "state": "OPEN",
            "body": "diagnostic\n" * 200,
            "repository": {"__typename": "Repository", "id": "R", "nameWithOwner": "o/r"},
            "comments": lambda info, **args: self.page(
                [
                    {
                        "__typename": "IssueComment",
                        "id": f"C{number}_{i}",
                        "body": f"comment {i}\nmore {i}\n",
                        "author": {"__typename": "User", "id": "U", "login": "alice"},
                    }
                    for i in range(1, 8)
                ],
                args,
            ),
            "files": lambda info, **args: self.page(
                [
                    {
                        "path": f"f{i}.py",
                        "additions": i,
                        "deletions": 0,
                        "changeType": "MODIFIED",
                        "viewerViewedState": "UNVIEWED",
                    }
                    for i in range(1, self.count + 1)
                ],
                args,
            ),
        }

    def repo(self, info, **args):
        return {
            "__typename": "Repository",
            "id": "R",
            "name": "r",
            "nameWithOwner": "o/r",
            "issue": lambda info, number: self.issue(number),
            "pullRequest": lambda info, number: self.issue(number, "PullRequest"),
            "issues": lambda info, **args: self.page([self.issue(i) for i in range(1, self.count + 1)], args),
        }

    def search(self, info, **args):
        if args["type"] == "REPOSITORY":
            items = [self.repo(None)]
        elif args["type"] == "USER":
            items = [{"__typename": t, "id": t, "login": t.lower(), "name": t} for t in ("User", "Organization")]
        else:
            kind = "PullRequest" if "is:pr" in args["query"] else "Issue"
            items = [self.issue(i, kind) for i in range(1, self.count + 1)]
            if "mixed" in args["query"]:
                items[0] = self.issue(1, "PullRequest")
        return self.page(items, args)

    async def __call__(self, request):
        self.requests.append(request)
        if request.method == "POST":
            query = json.loads(request.content)["query"]
            if self.fail_after and f'after: "{self.fail_after}"' in query:
                return httpx.Response(403, json={"message": "denied"})
            result = graphql_sync(
                public_schema(),
                query,
                root_value={
                    "repository": self.repo,
                    "search": self.search,
                    "node": lambda info, **args: self.issue(
                        int(args["id"][1:]), "PullRequest" if args["id"].startswith("P") else "Issue",
                    ),
                },
            )
            assert not result.errors, result.errors
            return httpx.Response(200, json=result.formatted)
        path = request.url.path
        if "/contents/" in path:
            return httpx.Response(
                200,
                json={
                    "name": "notes.md",
                    "size": 18,
                    "encoding": "base64",
                    "content": base64.b64encode(b"first\nsecond\nthird\n").decode(),
                },
            )
        if path.endswith("/issues/1"):
            return httpx.Response(
                200,
                json={
                    "number": 1,
                    "title": "title 1",
                    "comments": 7,
                    "comments_url": "https://api.github.com/repos/o/r/issues/1/comments",
                },
            )
        if path.endswith("/comments"):
            items = [
                {
                    "body": f"comment {i}",
                    "user": {"login": "alice", "name": "Alice"},
                    "url": f"https://api.github.com/comments/{i}",
                    "html_url": f"https://github.com/c/{i}",
                }
                for i in range(1, 8)
            ]
        elif path.endswith("/files"):
            items = [
                {"filename": f"f{i}.py", "status": "modified", "patch": f"+line {i}", "additions": i, "deletions": 0}
                for i in range(1, self.count + 1)
            ]
        else:
            items = [
                {
                    "name": f"f{i}.py",
                    "path": f"f{i}.py",
                    "sha": f"sha{i}",
                    "url": f"https://api.github.com/files/{i}",
                    "html_url": f"https://github.com/f{i}",
                }
                for i in range(1, self.count + 1)
            ]
        page, size = int(request.url.params.get("page", 1)), int(request.url.params.get("per_page", 30))
        if self.fail_after and page > 1:
            return httpx.Response(403, json={"message": "denied"})
        last, links = max(1, math.ceil(len(items) / size)), {}
        if page < last:
            links.update(next=page + 1, last=last)
        if page > 1:
            links.update(prev=page - 1, first=1)
        headers = (
            {"Link": ", ".join(f'<{request.url.copy_set_param("page", p)}>; rel="{rel}"' for rel, p in links.items())}
            if links
            else {}
        )
        selected = items[(page - 1) * size : page * size]
        body = (
            {"items": selected, "total_count": len(items), "incomplete_results": False}
            if path.startswith("/search/")
            else selected
        )
        return httpx.Response(200, json=body, headers=headers)


class Server:
    def __init__(self):
        self.requests = []
        self.fail_page = None
        self.fail_comments = None

    def issue(self, number):
        return {
            "number": str(number),
            "title": f"Issue {number}",
            "body": "diagnostic log\n" * 5000,
            "user": {"login": "alice", "name": "Alice"},
            "comments": 5,
            "repository": {"full_name": "o/r"},
            "created_at": "2026-09-01",
            "html_url": f"https://gitcode.com/o/r/issues/{number}",
        }

    def page(self, items, request):
        page = int(request.url.params.get("page", 1))
        size = int(request.url.params.get("per_page", 20))
        if self.fail_page == page:
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(
            200,
            json=items[(page - 1) * size : page * size],
            headers={"total_count": str(len(items)), "total_page": str((len(items) + size - 1) // size)},
        )

    async def __call__(self, request):
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v5")
        if path.endswith("/comments"):
            if self.fail_comments:
                return httpx.Response(self.fail_comments, json={"message": "denied"})
            values = [
                {"id": n, "body": f"comment {n}\n" + "text " * 200, "user": {"login": "alice", "name": "Alice"}}
                for n in range(1, 6)
            ]
            if request.url.params.get("order", request.url.params.get("direction")) == "desc":
                values.reverse()
            return self.page(values, request)
        if path.endswith("/pull_requests"):
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 9,
                        "title": "Related",
                        "body": "Not a closure claim",
                        "html_url": "https://gitcode.com/o/r/merge_requests/9",
                    },
                ],
            )
        if path.endswith("/contents/a.txt"):
            return httpx.Response(
                200,
                json={
                    "name": "a.txt",
                    "path": "a.txt",
                    "encoding": "base64",
                    "content": base64.b64encode(b"one\ntwo\nthree\n").decode(),
                },
            )
        if path == "/repos/o/r":
            return httpx.Response(200, json={"name": "r", "full_name": "o/r", "description": "test"})
        if path in {"/repos/o/r/issues", "/search/issues"}:
            return self.page([self.issue(n) for n in range(1, 4)], request)
        if path == "/repos/o/r/pulls":
            return self.page([{"number": 9, "title": "PR", "merged": True}], request)
        if path in {"/search/users", "/search/repositories"}:
            return self.page([], request)
        if "/issues/" in path:
            return httpx.Response(200, json=self.issue(path.rsplit("/", 1)[-1]))
        raise AssertionError(f"Unexpected native request: {request.url}")
