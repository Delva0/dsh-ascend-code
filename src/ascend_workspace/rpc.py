"""One container-side JSON operation; detached supervisors outlive the caller."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .vendor import sandbox_worker as worker
from .vendor.shell_contract import (
    auto_background_allowed,
    command_result,
    effective_timeout,
    normalize_arguments,
)


def session_root(session):
    """Map a session to its durable task directory without permitting path traversal."""
    if not isinstance(session, str) or not session:
        raise ValueError("session must be a nonempty string")
    root = Path(os.environ.get("ASCEND_TASK_ROOT", "/workspace/.dsh-ascend/tasks"))
    root = root / hashlib.sha256(session.encode()).hexdigest()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def present(state):
    """Retain execution facts and complete log paths alongside the bounded model view."""
    if state["status"] not in worker.TERMINAL:
        content = (
            f"Command running in background with ID: {state['task_id']}. "
            f"Output is being written to: {state['output_file']}"
        )
        return {**state, "content": content, "failed": False}
    failed, explanation = command_result(state["command"], state.get("exit_code"), state["output"])
    if state["status"] != "completed":
        failed = True
        explanation = state.get("reason") or f"Command {state['status']} before completion"
    content = state["output"].strip("\n")
    if state.get("persisted_output"):
        content = (
            f"Output ({state['output_bytes']} bytes) saved to: {state['persisted_output']}\n"
            f"Complete log: {state['output_file']}\nPreview (head and tail):\n{state['preview']}"
        )
        if state.get("persisted_truncated"):
            content += "\nSaved copy capped at 64 MiB; the complete live log is at output_file."
    if explanation:
        content = explanation + ("\n" + content if content else "")
    return {**state, "content": content, "failed": failed}


def execute(request):
    """Dispatch a fixed operation without executing caller-supplied Python."""
    action = request["action"]
    if action == "info":
        workdir = Path(request["workdir"]).resolve(strict=True)
        if not workdir.is_dir():
            raise ValueError("workdir must be a directory")
        repositories = []
        for name in ("vllm", "vllm-ascend"):
            path = workdir / name
            revision = subprocess.check_output(
                ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
            ).strip()
            repositories.append({"name": name, "path": str(path), "revision": revision})
        version = subprocess.check_output(["lsp", "--version"], text=True).strip()
        return {
            "workdir": str(workdir),
            "repositories": repositories,
            "lsp": version,
            "runtime": worker.runtime_id(),
        }
    root = session_root(request["session"])
    if action == "list":
        return [
            present(worker.shell_observe(path, output_limit=2000))
            for path in sorted(root.iterdir())
            if (path / "state.json").is_file()
            and (path / "notify").exists()
            and not (path / "notified").exists()
        ]
    task_id = request["task_id"]
    if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise ValueError("Invalid task ID")
    if action in {"track", "ack"}:
        task = worker.task_path(root, task_id)
        (task / ("notify" if action == "track" else "notified")).touch()
        return {"ok": True}
    maximum = request.get("max_output_bytes", 100000)
    if type(maximum) is not int or not 1 <= maximum <= 150000:
        raise ValueError("max_output_bytes must be an integer between 1 and 150000")
    args = {"task_root": str(root), "task_id": task_id, "wait": 0, "output_limit": maximum}
    if action == "start":
        values = normalize_arguments("bash", request["arguments"])
        if not isinstance(values, dict) or set(values) - {
            "command",
            "timeout",
            "description",
            "run_in_background",
        }:
            raise ValueError("Invalid bash arguments")
        if not isinstance(values.get("command"), str):
            raise ValueError("command must be a string")
        if values.get("description") is not None and not isinstance(values["description"], str):
            raise ValueError("description must be a string")
        if type(values.get("run_in_background", False)) is not bool:
            raise ValueError("run_in_background must be a boolean")
        if isinstance(values.get("timeout"), bool):
            raise ValueError("timeout must be a number")
        timeout = effective_timeout(
            values.get("timeout"), {"default_timeout_ms": 120000, "max_timeout_ms": 600000}
        )
        cwd = Path(request["workdir"]).resolve(strict=True)
        if not cwd.is_dir():
            raise ValueError("workdir must be a directory")
        args.update(
            action="shell_start",
            command=values["command"],
            description=values.get("description") or values["command"],
            cwd=str(cwd),
            timeout=timeout / 1000,
            background=values.get("run_in_background", False),
            auto_background=auto_background_allowed(values["command"]),
        )
    elif action in {"poll", "stop"}:
        args["action"] = "shell_poll" if action == "poll" else "shell_stop"
    else:
        raise ValueError(f"Unknown action: {action}")
    return present(worker.shell_request(args))


def main():
    """Emit only a structured protocol response on stdout."""
    try:
        response = {"result": execute(json.load(sys.stdin))}
    except Exception as error:
        response = {"error": {"type": type(error).__name__, "message": str(error)}}
    print(json.dumps(response, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
