"""dsh transport for the unchanged upstream web tools."""

import argparse
import asyncio
import math
import os
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from .core.registry import ToolRegistry, installed_specs
from .core.storage import ToolStorage
from .core.tool_web import DEFAULT_SEARCH_INTERVAL, SEARCH_BACKENDS, WebTools
from .http_client import http_client
from .worker import invoke, serve_calls


def definition(name="web_fetch"):
    _, spec = next(entry for entry in installed_specs(WebTools) if entry[0] == name)
    return {"name": spec.name, "description": spec.description,
            "parameters": spec.parameters, "returns": spec.returns}


def search_options():
    """Read explicitly forwarded host settings; retain the v5 search defaults."""
    backend = os.environ.get("DSH_ASCEND_WEB_SEARCH_BACKEND", "brave").strip()
    concurrency = int(os.environ.get("DSH_ASCEND_WEB_SEARCH_CONCURRENCY", "1"))
    interval = float(os.environ.get("DSH_ASCEND_WEB_SEARCH_INTERVAL", str(DEFAULT_SEARCH_INTERVAL)))
    if backend not in SEARCH_BACKENDS:
        raise ValueError("Unknown web search backend")
    if concurrency < 1 or not math.isfinite(interval) or interval < 0:
        raise ValueError("Search concurrency must be positive and interval finite and nonnegative")
    key = next((value for name in ("BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY")
                if (value := os.environ.get(name, "").strip())), "")
    return {"search_backend": backend, "search_concurrency": concurrency,
            "search_interval": interval, "brave_api_key": key}


class SearchSession:
    """One provider owns pacing, concurrency and cooldown across every search call."""

    def __init__(self, client, storage, **options):
        self.provider = WebTools(client, storage, is_llm_multi_modal=False, **options)
        self.registry = ToolRegistry(self.provider)

    async def invoke(self, _registry, call_id, arguments, _context):
        return await invoke(self.registry, call_id, arguments, name="web_search")


class FetchSession:
    """Share refs and the fetch limit across per-call image capability choices."""

    def __init__(self, client, storage):
        self.providers = {mode: WebTools(client, storage, is_llm_multi_modal=mode) for mode in (False, True)}
        plain, images = self.providers[False], self.providers[True]
        images.resources = plain.resources
        images.fetch_limit = plain.fetch_limit
        self.registries = {mode: ToolRegistry(provider) for mode, provider in self.providers.items()}

    async def invoke(self, _registry, call_id, arguments, context):
        mode = context.get("image_input", False)
        if type(mode) is not bool:
            raise ValueError("image_input must be a boolean")
        provider = self.providers[mode]
        reply = await invoke(self.registries[mode], call_id, arguments, name="web_fetch")
        parts = []
        # Use the original image projection, one item at a time. Carry only file
        # references over stdio so a valid 20 MiB image never hits the JSON limit.
        for item in reply["value"].get("results", []):
            if item.get("image_input"):
                _, observations = provider.image_parts({"results": [item]}, call_id)
                for part in observations:
                    if part["type"] == "input_image":
                        part = {**part, "image_url": str(provider.storage.path(part["image_url"]))}
                    parts.append(part)
        return {**reply, "image_parts": parts}


async def serve(backend="web_fetch"):
    with TemporaryDirectory(prefix="dsh-ascend-web-") as directory:
        # Each tool has its own worker/client. Only the search worker receives
        # Brave credentials; the original provider scopes its auth header to Brave.
        async with http_client(timeout=60.0) as client:
            storage = ToolStorage(Path(directory))
            session = (SearchSession(client, storage, **search_options()) if backend == "web_search"
                       else FetchSession(client, storage))
            await serve_calls(backend, None, session.invoke)


def main():
    parser = argparse.ArgumentParser(description="Native dsh web tool worker")
    parser.add_argument("--backend", choices=["web_fetch", "web_search"], default="web_fetch")
    args = parser.parse_args()
    with suppress(KeyboardInterrupt, BrokenPipeError, asyncio.CancelledError):
        asyncio.run(serve(args.backend))


if __name__ == "__main__":
    main()
