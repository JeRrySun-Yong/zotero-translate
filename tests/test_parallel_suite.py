"""The full suite runs in parallel processes, one test file per process (M3 ticket 18).

``pytest -n auto`` starts 3 processes (pytest-xdist); each test file runs
whole in one of them, so a module fixture runs once. Without ``-n`` the
suite runs serially. A test that leaves out ``--data-dir`` (``run``) or
``data_dir`` gets a data directory of its own, never the user's or another
test's, so no two tests share a model cache.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Each inner test writes down which process ran it.
INNER_TEST = """
import json
import os
from pathlib import Path

import pytest


@pytest.mark.parametrize("n", range(4))
def test_records_its_process(n, request):
    record = {
        "file": request.node.path.name,
        "worker": os.environ.get("PYTEST_XDIST_WORKER", "main"),
        "workers": os.environ.get("PYTEST_XDIST_WORKER_COUNT", "1"),
    }
    out = Path(os.environ["ZT_TEST_RECORDS"]) / f"{request.node.path.stem}-{n}.json"
    out.write_text(json.dumps(record))
"""


def _run_inner(tmp_path: Path, *options: str) -> list[dict]:
    inner = tmp_path / "inner"
    inner.mkdir()
    for name in ("test_one.py", "test_two.py", "test_three.py"):
        (inner / name).write_text(INNER_TEST, encoding="utf-8")
    records = tmp_path / "records"
    records.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST")}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests.conftest"]
        + ["-c", str(REPO / "pyproject.toml"), "--rootdir", str(inner), *options, str(inner)],
        cwd=REPO,
        env={**env, "ZT_TEST_RECORDS": str(records)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    return [json.loads(p.read_text()) for p in sorted(records.iterdir())]


def test_n_auto_runs_three_processes_and_keeps_each_file_in_one(tmp_path):
    records = _run_inner(tmp_path, "-n", "auto")

    assert len(records) == 12
    assert {r["workers"] for r in records} == {"3"}
    by_file: dict[str, set[str]] = {}
    for r in records:
        by_file.setdefault(r["file"], set()).add(r["worker"])
    assert all(len(workers) == 1 for workers in by_file.values()), by_file


def test_without_n_the_tests_run_in_one_process(tmp_path):
    records = _run_inner(tmp_path)

    assert len(records) == 12
    assert {r["worker"] for r in records} == {"main"}


def test_a_test_gets_a_default_data_dir_of_its_own(tmp_path):
    # Subprocesses (``run`` without ``--data-dir``) read the environment.
    assert Path(os.environ["ZT_DATA_DIR"]).parent == tmp_path
    # In this process the default was read at import, before any test.
    from zotero_translate.settings import DEFAULT_DATA_DIR

    assert not DEFAULT_DATA_DIR.is_relative_to(Path.home() / "Library")
