"""The plugin package tests pass in the public repository's layout.

The public repository has no plugin/dev/ (the development tools), docs/ or
tools/. This copies the plugin package tests, the plugin and the build
script into a tree of that shape and runs the tests there.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_plugin_package_tests_pass_without_the_development_tools(tmp_path):
    (tmp_path / "tests").mkdir()
    shutil.copy(ROOT / "tests" / "test_plugin_package.py", tmp_path / "tests")
    (tmp_path / "plugin").mkdir()
    shutil.copy(ROOT / "plugin" / "build.py", tmp_path / "plugin")
    shutil.copytree(ROOT / "plugin" / "addon", tmp_path / "plugin" / "addon")
    assert not (tmp_path / "plugin" / "dev").exists()

    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_plugin_package.py", "-rs"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout[-3000:]
    assert " passed" in run.stdout
    # The one test that reads a development file skips itself, with its reason.
    assert "the public repository holds no development tools" in run.stdout
    assert "failed" not in run.stdout and "error" not in run.stdout.lower().replace("errors", "")
