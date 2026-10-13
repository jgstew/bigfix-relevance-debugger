"""Runs the shared CodeMirror client's Node tests (tests/unit/upstream/web/).

Against NiceGUI's own CodeMirror bundle, the one the page uses. Skipped when
Node isn't installed; CI's ubuntu runners have it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import nicegui
import pytest

NODE = shutil.which("node")
TESTS = Path(__file__).parent / "upstream" / "web"
NICEGUI_CM = Path(nicegui.__file__).parent / "elements" / "codemirror" / "dist" / "index.js"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_shared_client_node_tests():
    assert NODE is not None
    files = sorted(str(p) for p in TESTS.glob("*.test.mjs"))
    assert files, f"no Node tests in {TESTS}"
    done = subprocess.run(
        [NODE, "--test", *files],
        env={**os.environ, "CM_MODULE": str(NICEGUI_CM)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
