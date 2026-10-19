"""Prepare public repositories once, preserving existing checkouts and complete history."""

import fcntl
import json
import os
import subprocess
from pathlib import Path


def prepare(root=Path("/workspace")):
    """Clone into temporary directories, then publish without overwriting user work."""
    metadata = root / ".dsh-ascend"
    metadata.mkdir(exist_ok=True)
    with (metadata / "prepare.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = []
        for name in ("vllm", "vllm-ascend"):
            url = f"https://github.com/vllm-project/{name}.git"
            path = root / name
            if not path.exists():
                pending = root / f".{name}-clone-{os.getpid()}"
                print(f"Cloning {url} with full history and tags", flush=True)
                subprocess.run(["git", "clone", url, str(pending)], check=True)
                pending.rename(path)
            actual = subprocess.check_output(
                ["git", "-C", str(path), "remote", "get-url", "origin"], text=True
            ).strip()
            if actual != url:
                raise ValueError(f"{path} already exists with a different origin: {actual}")
            revision = subprocess.check_output(
                ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
            ).strip()
            shallow = subprocess.check_output(
                ["git", "-C", str(path), "rev-parse", "--is-shallow-repository"], text=True
            ).strip()
            if shallow != "false":
                raise ValueError(f"{path} is shallow; fetch the full history before using this preset")
            manifest.append({"path": str(path), "origin": actual, "prepared_head": revision})
        (metadata / "repositories.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(json.dumps(manifest, indent=2), flush=True)


if __name__ == "__main__":
    prepare()
