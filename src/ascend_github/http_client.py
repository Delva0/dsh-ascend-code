"""HTTPX compatibility for the environment inherited from the dsh launcher."""

import os
import re
from ipaddress import IPv6Address

import httpx


def _no_proxy(value):
    entries = []
    for entry in value.split(","):
        host = entry.strip()
        if match := re.fullmatch(r"\[([^\]]+)\](?::[0-9]+)?", host):
            try:
                IPv6Address(match[1])
            except ValueError:
                pass
            else:
                # HTTPX accepts URL patterns, but mistakes [::1] for a domain
                # and prepends '*'. Keep the exact host and optional port.
                entry = f"all://{host}"
        entries.append(entry)
    return ",".join(entries)


def http_client(*, timeout):
    """Construct once at worker startup, before starting tasks or DDG threads.

    Only the synchronous HTTPX constructor sees normalized IPv6 bypass entries.
    Restore the environment before returning, including on failure, so gh/DDG
    and child processes retain dsh's original policy. HTTPX still resolves the
    proxies, casing precedence, wildcard bypass and CA settings itself.
    """
    previous = {name: os.environ[name] for name in ("NO_PROXY", "no_proxy") if name in os.environ}
    try:
        for name, value in previous.items():
            os.environ[name] = _no_proxy(value)
        return httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    finally:
        os.environ.update(previous)
