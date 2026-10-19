"""Private JSON-lines bridge for one dsh agent's native GitHub tool.

Each process owns an HTTP client, response cache and temporary evidence files.
An acknowledged cancellation means the corresponding coroutine has settled.
"""

import argparse
import asyncio
import json
import math
import os
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from jsonschema import ValidationError

from .core.registry import ToolRegistry, input_error, installed_specs
from .core.storage import ToolStorage
from .core.tool_github import GitHubDSLTool, GitHubGraphQLTool, GitHubRESTTool

BACKENDS = {"rest": GitHubRESTTool, "graphql": GitHubGraphQLTool, "dsl": GitHubDSLTool}
MAX_FRAME_BYTES = 8 * 1024 * 1024


def definition(backend):
    """Export the original backend contract for the native tool registration."""
    _, spec = installed_specs(BACKENDS[backend], "github")[0]
    return {"name": "github", "description": spec.description,
            "parameters": spec.parameters, "returns": spec.returns}


def github_token():
    """Resolve host credentials without putting a token in argv or configuration."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"):
        if value := os.environ.get(name, "").strip():
            return value
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


async def invoke(registry, call_id, arguments, context=None, *, name="github"):
    """Preserve per-item batch validation and structured backend failures."""
    try:
        value = await registry.call(name, call_id, arguments)
    except (ValidationError, ValueError) as error:
        value = {"error": input_error(error)}
    items = value.get("results", [])
    failed = bool(value.get("error") or value.get("errors")
                  or (items and all(item.get("error") for item in items)))
    return {"value": value, "failed": failed}


async def dispatch(reader, send, registry, invoke_call=invoke):
    """Multiplex calls; EOF cancels and drains all work before releasing resources."""
    tasks = {}

    async def call(message):
        identity = message["id"]
        try:
            reply = await invoke_call(registry, message.get("call_id", identity), message.get("arguments"),
                                      message.get("context", {}))
        except asyncio.CancelledError:
            reply = {"cancelled": True}
        except Exception as error:
            reply = {"value": {"error": {"type": type(error).__name__, "message": str(error)}},
                     "failed": True}
        send({"id": identity, **reply})

    def settled(identity, task):
        tasks.pop(identity, None)
        # Cancellation can arrive before the task's first instruction.
        if task.cancelled():
            send({"id": identity, "cancelled": True})
        elif task.exception() is not None:
            # A broken output stream must stop the worker, not leave callers hanging.
            reader.set_exception(task.exception())

    try:
        while line := await reader.readline():
            if len(line) > MAX_FRAME_BYTES:
                raise ValueError("GitHub request exceeds the bridge frame limit")
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("GitHub bridge expects a JSON object")
            if "cancel" in message:
                task = tasks.get(message["cancel"])
                if task is not None and not task.cancelling():
                    task.cancel()
                continue
            identity = message.get("id")
            if not isinstance(identity, str) or not identity or identity in tasks:
                raise ValueError("GitHub bridge requires a unique nonempty request id")
            task = asyncio.create_task(call(message))
            tasks[identity] = task
            task.add_done_callback(lambda done, key=identity: settled(key, done))
    finally:
        pending = list(tasks.values())
        for task in pending:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def serve_calls(backend, registry, invoke_call=invoke):
    loop = asyncio.get_running_loop()
    # dsh terminates managed workers with TERM before its forced-kill grace.
    # Convert TERM into structured cancellation so caches and HTTP clients close.
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer,
    )

    def send(value):
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > MAX_FRAME_BYTES:
            raise ValueError("GitHub response exceeds the bridge frame limit")
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()

    try:
        send({"ready": True, "backend": backend})
        await dispatch(reader, send, registry, invoke_call)
    finally:
        transport.close()
        loop.remove_signal_handler(signal.SIGTERM)


async def serve(backend, token, concurrency=8, http_timeout=60.0):
    with TemporaryDirectory(prefix="dsh-ascend-github-") as directory:
        async with httpx.AsyncClient(timeout=http_timeout, follow_redirects=False) as client:
            provider = BACKENDS[backend](client, ToolStorage(Path(directory)),
                                        token=token, concurrency=concurrency)
            await serve_calls(backend, ToolRegistry((provider, "github")))


def main():
    parser = argparse.ArgumentParser(description="Native dsh GitHub worker (private JSON-lines protocol)")
    parser.add_argument("--backend", choices=BACKENDS, default="rest")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    if args.concurrency < 1 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("concurrency and timeout must be positive finite numbers")
    with suppress(KeyboardInterrupt, BrokenPipeError, asyncio.CancelledError):
        asyncio.run(serve(args.backend, github_token(), args.concurrency, args.timeout))


if __name__ == "__main__":
    main()
