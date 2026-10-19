"""dsh transport for the unchanged upstream web_fetch provider."""

import argparse
import asyncio
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from .core.registry import ToolRegistry, installed_specs
from .core.storage import ToolStorage
from .core.tool_web import WebTools
from .worker import invoke, serve_calls


def definition():
    _, spec = next(entry for entry in installed_specs(WebTools) if entry[0] == "web_fetch")
    return {"name": spec.name, "description": spec.description,
            "parameters": spec.parameters, "returns": spec.returns}


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


async def serve():
    with TemporaryDirectory(prefix="dsh-ascend-web-") as directory:
        # An independent unauthenticated client: GitHub credentials never enter
        # this process, its default headers, or external redirect requests.
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
            session = FetchSession(client, ToolStorage(Path(directory)))
            await serve_calls("web_fetch", None, session.invoke)


def main():
    parser = argparse.ArgumentParser(description="Native dsh web_fetch worker")
    parser.add_argument("--backend", choices=["web_fetch"], default="web_fetch")
    parser.parse_args()
    with suppress(KeyboardInterrupt, BrokenPipeError, asyncio.CancelledError):
        asyncio.run(serve())


if __name__ == "__main__":
    main()
