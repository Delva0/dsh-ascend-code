"""HTTPX must preserve proxy routing while accepting dsh's IPv6 bypass list."""

import asyncio
import os
from contextlib import asynccontextmanager

import pytest

from ascend_github.http_client import http_client


@pytest.fixture(autouse=True)
def clean_proxies(monkeypatch):
    for name in list(os.environ):
        if name.lower().endswith("_proxy") or name == "REQUEST_METHOD":
            monkeypatch.delenv(name)


@asynccontextmanager
async def endpoint(host, body):
    received = []

    async def respond(reader, writer):
        try:
            received.append((await reader.readuntil(b"\r\n\r\n")).decode())
            writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        server = await asyncio.start_server(respond, host, 0)
    except OSError:
        if host == "::1":
            pytest.skip("IPv6 loopback is unavailable on this runner")
        raise
    async with server:
        yield server.sockets[0].getsockname()[1], received


@pytest.mark.parametrize("bypass", ["[::1]", "::1,[::1]", "all://[::1]", "[::1]:{port}"])
async def test_ipv6_bypass_keeps_host_and_optional_port(monkeypatch, bypass):
    async with endpoint("::1", b"direct") as (port, _), endpoint("127.0.0.1", b"proxy") as (proxy, seen):
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy}")
        monkeypatch.setenv("NO_PROXY", bypass.format(port=port))
        original = dict(os.environ)
        async with http_client(timeout=2) as client:
            assert dict(os.environ) == original
            assert (await client.get(f"http://[::1]:{port}/direct")).text == "direct"
            assert (await client.get("http://upstream.invalid/proxy")).text == "proxy"
            if "{port}" in bypass:
                # A different port must still go through the proxy.
                other = port % 65535 + 1
                assert (await client.get(f"http://[::1]:{other}/port")).text == "proxy"
                assert f"Host: [::1]:{other}\r\n" in seen[-1]
        assert len(seen) == (2 if "{port}" in bypass else 1)


@pytest.mark.parametrize(("upper", "lower", "route"), [
    ("[::1]", None, "proxy"),
    ("[::1],*", None, "direct"),
    ("*", "[::1]", "proxy"),
    ("[::1]", "*", "direct"),
    ("*", "", "proxy"),
    ("[::1],localhost,127.0.0.1", None, "direct"),
    (None, None, "proxy"),
])
async def test_bypass_precedence_wildcard_and_auth_are_preserved(monkeypatch, upper, lower, route):
    async with (
        endpoint("127.0.0.1", b"direct") as (port, direct),
        endpoint("127.0.0.1", b"proxy") as (proxy, seen),
    ):
        monkeypatch.setenv("HTTP_PROXY", f"http://fixture:password@127.0.0.1:{proxy}")
        if upper is not None:
            monkeypatch.setenv("NO_PROXY", upper)
        if lower is not None:
            monkeypatch.setenv("no_proxy", lower)
        original = dict(os.environ)
        async with http_client(timeout=2) as client:
            assert dict(os.environ) == original
            assert client.trust_env
            assert (await client.get(f"http://127.0.0.1:{port}/request")).text == route
        assert dict(os.environ) == original
        if route == "proxy":
            assert not direct
            assert "Proxy-Authorization: Basic Zml4dHVyZTpwYXNzd29yZA==" in seen[0]
        else:
            assert not seen
            assert "proxy-authorization" not in direct[0].lower()


def test_constructor_failure_restores_original_environment(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "[::1],localhost")
    monkeypatch.setenv("no_proxy", "[2001:db8::1]:8443, example.test,*,https://[::1]")
    # Keep HTTPX's usual CA configuration rather than bypassing trust_env.
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/dsh-ascend-test-ca.pem")
    original = dict(os.environ)
    with pytest.raises(FileNotFoundError):
        http_client(timeout=2)
    assert dict(os.environ) == original
