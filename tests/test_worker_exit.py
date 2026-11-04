"""The worker and ``run`` end at once after their work, losing nothing (M3 ticket 17).

A native library's exit handler sometimes hangs after Python's own shutdown.
Here ``tests/hang_at_exit/sitecustomize.py`` registers such a handler (a C
``atexit`` that never returns) in the process under test, which must
therefore end by ``os._exit``. Each test then checks that nothing was lost:
the exit code, the result report, the task report's request records against
``requests.jsonl``, and that no process of the task is left behind.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from tests.conftest import EXCERPT
from tests.conftest import FAKE_API_KEY
from tests.conftest import FAKE_ARGON_API_KEY
from tests.conftest import FAKE_MODEL_NAME
from tests.conftest import FIXTURES
from tests.conftest import MARK_ENV
from tests.conftest import marked_processes
from tests.conftest import wait_until
from tests.conftest import write_env_file
from tests.fake_model import FakeModel
from tests.fake_model import pseudo_translate_algorithm
from zotero_translate import task_dir as td
from zotero_translate.request_log import REQUESTS_FILE
from zotero_translate.request_log import load_records

HANG_AT_EXIT = {
    "PYTHONPATH": str(Path(__file__).parent / "hang_at_exit"),
    "ZT_TEST_HANG_AT_EXIT": "1",
}
PAPER_SECONDS = 240  # how long a paper task may take; a process hanging at exit never ends
LATE_SECONDS = 4.0  # how long the late algorithm answer takes


def _env_files(model: FakeModel, root: Path, timeout: float | None = None) -> list[str]:
    flash = write_env_file(root, model.base_url, FAKE_API_KEY, FAKE_MODEL_NAME, timeout)
    argon = write_env_file(root, model.base_url, FAKE_ARGON_API_KEY, None, timeout)
    return ["--env-file", str(flash), "--argon-env-file", str(argon)]


def _run_worker(
    tmp_path: Path, task_id: str, env_files: list[str], extra_env: dict | None = None, args=None
) -> tuple[int, list[dict], Path]:
    """Run the worker on EXCERPT as the service does; return its exit code, reports, task dir."""
    directory = tmp_path / "tasks" / task_id
    directory.mkdir(parents=True)
    (directory / td.SOURCE_PDF).write_bytes(EXCERPT.read_bytes())
    read_end, write_end = os.pipe()
    command = [sys.executable, "-m", "zotero_translate.worker", str(directory)]
    command += args if args is not None else ["--attempt", "1", "--report-fd", str(write_end)]
    command += env_files + ["--concurrency", "4", "--cache-dir", str(tmp_path / "cache")]
    with (directory / td.LOG_FILE).open("ab") as log:
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            pass_fds=(write_end,),
            env={**os.environ, **HANG_AT_EXIT, **(extra_env or {}), MARK_ENV: f"{task_id}:1"},
            start_new_session=True,
        )
    os.close(write_end)
    reports: list[dict] = []

    def read() -> None:
        with os.fdopen(read_end, encoding="utf-8") as pipe:
            reports.extend(json.loads(line) for line in pipe)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        code = process.wait(timeout=PAPER_SECONDS)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        reader.join()
    return code, reports, directory


def _run_command(tmp_path: Path, pdf: Path, env_files: list[str]) -> tuple[int, Path]:
    """Run ``zotero-translate run``; return its exit code and output directory."""
    output_dir = tmp_path / "out"
    command = Path(sys.executable).with_name("zotero-translate")
    process = subprocess.Popen(
        [str(command), "run", str(pdf), "--output-dir", str(output_dir)]
        + env_files
        + ["--data-dir", str(tmp_path / "data")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, **HANG_AT_EXIT},
        start_new_session=True,
    )
    try:
        code = process.wait(timeout=PAPER_SECONDS)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return code, output_dir


def _assert_records_whole(report_path: Path) -> list[dict]:
    report = json.loads(report_path.read_text("utf-8"))
    records = load_records(report_path.parent / REQUESTS_FILE)
    assert records
    assert report["requests"] == records
    return records


def _assert_no_process_left(task_id: str) -> None:
    wait_until(lambda: not marked_processes([task_id]), timeout=10)


def test_a_worker_that_succeeds_ends_at_once_with_its_records_and_children_gone(tmp_path):
    with FakeModel() as model:
        code, reports, directory = _run_worker(
            tmp_path, "t17ok", _env_files(model, tmp_path), {"ZT_TEST_WORKER_SPAWN_CHILD": "1"}
        )

    assert code == 0, (directory / td.LOG_FILE).read_text("utf-8")[-3000:]
    assert reports[-1]["type"] == "result" and reports[-1]["outcome"] == "succeeded"
    report_path = td.attempt_report_path(directory, 1)
    assert json.loads(report_path.read_text("utf-8"))["state"] == "succeeded"
    _assert_records_whole(report_path)
    _assert_no_process_left("t17ok")


def test_a_worker_whose_paper_task_fails_ends_at_once_with_exit_code_1(tmp_path):
    with FakeModel() as model:
        model.inject_error("401", kind="term_list")
        code, reports, directory = _run_worker(tmp_path, "t17fail", _env_files(model, tmp_path))

    assert code == 1
    assert reports[-1]["type"] == "result"
    assert (reports[-1]["outcome"], reports[-1]["category"]) == ("failed", "permanent")
    report_path = td.attempt_report_path(directory, 1)
    assert json.loads(report_path.read_text("utf-8"))["state"] == "failed"
    records = _assert_records_whole(report_path)
    assert [(r["kind"], r["result"]) for r in records] == [
        ("dossier", "succeeded"),
        ("term_list", "http_error"),
    ]
    _assert_no_process_left("t17fail")


def test_a_worker_that_cannot_read_its_settings_ends_at_once_with_its_child_gone(tmp_path):
    missing = ["--env-file", str(tmp_path / "missing.env"), "--argon-env-file", str(tmp_path / "x")]
    code, reports, _ = _run_worker(
        tmp_path, "t17settings", missing, {"ZT_TEST_WORKER_SPAWN_CHILD": "1"}
    )

    assert code == 1
    assert [(r["type"], r["outcome"], r["category"]) for r in reports] == [
        ("result", "failed", "permanent")
    ]
    _assert_no_process_left("t17settings")


def test_a_worker_given_bad_arguments_ends_at_once_with_exit_code_2(tmp_path):
    code, reports, _ = _run_worker(tmp_path, "t17args", [], args=["--attempt", "x"])

    assert code == 2
    assert reports == []


def test_run_that_succeeds_ends_at_once_with_its_records(tmp_path):
    with FakeModel() as model:
        code, output_dir = _run_command(tmp_path, EXCERPT, _env_files(model, tmp_path))

    assert code == 0
    assert json.loads((output_dir / "report.json").read_text("utf-8"))["state"] == "succeeded"
    _assert_records_whole(output_dir / "report.json")


def _late_algorithm(body: dict) -> str:
    time.sleep(LATE_SECONDS)
    return pseudo_translate_algorithm(body)


def test_run_that_fails_with_a_send_in_flight_ends_at_once_with_that_send_in_its_report(
    tmp_path,
):
    # The algorithm send outlives the failure report; ``run`` must bring
    # report.json up to date from requests.jsonl before it ends.
    with FakeModel() as model:
        model.respond("algorithm", _late_algorithm)
        model.inject_error("401", kind="translate_batch")
        env_files = _env_files(model, tmp_path, timeout=LATE_SECONDS * 2)
        code, output_dir = _run_command(tmp_path, FIXTURES / "maliang-p6.pdf", env_files)
        assert model.requests_of("algorithm")

    assert code == 1
    assert json.loads((output_dir / "report.json").read_text("utf-8"))["state"] == "failed"
    records = _assert_records_whole(output_dir / "report.json")
    failed = [r for r in records if r["http_status"] == 401]
    late = [r for r in records if r["kind"] == "algorithm_translation" and r["send"] == 1]
    assert len(failed) == 1 and len(late) == 1, records
    assert late[0]["started_at"] < failed[0]["started_at"]


EXIT_SCRIPT = """
import atexit, sys, threading, time
from pathlib import Path
from zotero_translate.process_exit import run_then_exit

out = Path(sys.argv[1])

def late_record():
    time.sleep(0.5)
    (out / "record").write_text("written")

def exit_handler():
    (out / "handler").write_text("after record" if (out / "record").exists() else "before record")

def main():
    threading.Thread(target=late_record).start()
    atexit.register(exit_handler)
    print("main done")
    if sys.argv[2] == "raise":
        raise ValueError("broken")
    return int(sys.argv[2])
"""


def _exit_script(tmp_path: Path, how: str) -> subprocess.CompletedProcess:
    script = tmp_path / "script.py"
    script.write_text(EXIT_SCRIPT + "\nrun_then_exit(main)\n", encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script), str(tmp_path), how],
        capture_output=True,
        text=True,
        env={**os.environ, **HANG_AT_EXIT},
        timeout=30,
    )


def test_the_exit_joins_the_threads_then_runs_the_exit_handlers_then_ends_at_once(tmp_path):
    result = _exit_script(tmp_path, "7")

    assert result.returncode == 7
    assert result.stdout == "main done\n"  # flushed, though stdout is a pipe
    assert (tmp_path / "record").read_text() == "written"
    assert (tmp_path / "handler").read_text() == "after record"


def test_an_exception_out_of_main_is_printed_and_ends_the_process_with_1(tmp_path):
    result = _exit_script(tmp_path, "raise")

    assert result.returncode == 1
    assert "ValueError: broken" in result.stderr
    assert (tmp_path / "handler").read_text() == "after record"
