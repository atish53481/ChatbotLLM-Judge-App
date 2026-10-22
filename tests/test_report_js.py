"""Runs the web app's JavaScript report tests (tests/js) with Node's built-in
test runner, so the offline suite and CI cover the PDF report too.

Skipped when Node is not installed; GitHub's ubuntu runners ship with it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS_TESTS = sorted((Path(__file__).parent / "js").glob("*.test.js"))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_report_javascript():
    assert JS_TESTS, "no JavaScript tests found in tests/js"
    result = subprocess.run(
        ["node", "--test", *map(str, JS_TESTS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
