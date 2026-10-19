"""Exercise real supervisor processes and persisted task state without model calls."""

import hashlib
import json
import time
import uuid
from pathlib import Path

import pytest

from ascend_workspace.rpc import execute
from ascend_workspace.vendor.sandbox_worker import TERMINAL


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ASCEND_TASK_ROOT", str(tmp_path / "tasks"))
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    calls = []

    def call(action="start", *, task_id=None, session="test", **values):
        request = {
            "action": action,
            "task_id": task_id or uuid.uuid4().hex,
            "session": session,
            "workdir": str(workdir),
            **values,
        }
        if action == "start":
            calls.append(request)
        return execute(request)

    yield call, workdir
    for request in calls:
        try:
            execute({**request, "action": "stop"})
        except ValueError:
            pass


def settled(call, state):
    """Wait only for an owned task with a bounded assertion deadline."""
    deadline = time.monotonic() + 5
    while state["status"] not in TERMINAL:
        assert time.monotonic() < deadline, state
        time.sleep(0.05)
        state = call("poll", task_id=state["task_id"])
    return state


def test_shell_and_cwd_contract(runtime):
    call, workdir = runtime
    (workdir / "child").mkdir()
    state = settled(call, call(arguments={"command": "cd child && export ASCEND_PROBE=yes && pwd"}))
    assert state["output"].strip() == str(workdir / "child")
    other = settled(call, call(arguments={"command": 'pwd; printf "%s" "${ASCEND_PROBE-unset}"'}))
    assert other["output"].splitlines() == [str(workdir), "unset"]


def test_long_output_is_recoverable(runtime):
    call, _ = runtime
    state = settled(call, call(arguments={"command": "printf '%0200000d' 0"}))
    assert state["truncated"]
    assert len(Path(state["output_file"]).read_bytes()) == 200000
    assert Path(state["persisted_output"]).exists()


@pytest.mark.parametrize(("command", "failed"), [("exit 7", True), ("rg missing /dev/null", False)])
def test_exit_semantics(runtime, command, failed):
    call, _ = runtime
    assert settled(call, call(arguments={"command": command}))["failed"] is failed


def test_timeout_promotes_then_completes(runtime):
    call, _ = runtime
    state = call(arguments={"command": "sleep 0.3; printf done", "timeout": 20})
    time.sleep(0.15)
    state = call("poll", task_id=state["task_id"])
    assert state["background"]
    state = settled(call, state)
    assert state["output"] == "done"


def test_stop_and_foreign_session(runtime):
    call, _ = runtime
    state = call(arguments={"command": "sleep 30", "run_in_background": True})
    with pytest.raises(ValueError, match="No task"):
        call("poll", task_id=state["task_id"], session="different")
    state = settled(call, call("stop", task_id=state["task_id"]))
    assert state["status"] == "stopped"


def test_notification_recovery_and_ack(runtime):
    call, _ = runtime
    state = settled(call, call(arguments={"command": "printf persisted", "run_in_background": True}))
    call("track", task_id=state["task_id"])
    assert call("list")[0]["output"] == "persisted"
    call("ack", task_id=state["task_id"])
    assert call("list") == []


def test_unknown_runtime_marks_interrupted(runtime):
    call, _ = runtime
    state = settled(call, call(arguments={"command": "true"}))
    path = Path(state["output_file"]).with_name("state.json")
    saved = json.loads(path.read_text())
    saved.update(status="running", runtime="different-container")
    path.write_text(json.dumps(saved))
    state = call("poll", task_id=state["task_id"])
    assert state["status"] == "interrupted"
    assert state["exit_code"] is None


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        [],
        {},
        {"command": 2},
        {"command": "true", "workdir": "/"},
        {"command": "true", "timeout": True},
        {"command": "true", "timeout": "NaN"},
        {"command": "true", "run_in_background": 3},
    ],
)
def test_invalid_arguments(runtime, arguments):
    call, _ = runtime
    with pytest.raises(ValueError):
        call(arguments=arguments)


def test_upstream_files_are_unchanged():
    root = Path(__file__).resolve().parents[1]
    for item in json.loads((root / "UPSTREAM.json").read_text())["files"]:
        assert hashlib.sha256((root / item["path"]).read_bytes()).hexdigest() == item["sha256"]
