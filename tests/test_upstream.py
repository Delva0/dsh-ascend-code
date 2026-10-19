"""Prevent silent changes to imported implementations and prompts."""

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = json.loads((ROOT / "UPSTREAM.json").read_text())["files"]


@pytest.mark.parametrize("entry", FILES, ids=lambda entry: entry["path"])
def test_imported_source_matches_locked_upstream(entry):
    assert "changes" not in entry
    assert hashlib.sha256((ROOT / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
