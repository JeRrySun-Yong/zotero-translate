"""Subprocess entry point: run one attempt of a paper task in its task directory.

Usage: python -m zotero_translate.worker TASK_DIR --attempt N --report-fd FD
--env-file FILE --argon-env-file FILE --concurrency N [--cache-dir DIR]

The subprocess never writes the status file: only the translation service
decides the task's state. It reports to the service through the pipe ``FD``,
one JSON object per line, each with the attempt number:

- ``{"attempt": n, "type": "progress", "stage": ..., "progress": ...}``;
- ``{"attempt": n, "type": "result", "outcome": "succeeded",
  "kept_source_paragraphs": ..., "kept_source_algorithms": ...}``;
- ``{"attempt": n, "type": "result", "outcome": "failed", "error": ...,
  "category": "permanent" | "retryable"}``: ``permanent`` when the settings
  cannot be read, or when the paper task failed with that category (a
  credential error, carried from the model call through the engine's
  failure summary); ``retryable`` otherwise.

The attempt's task report goes to ``attempts/<n>/report.json``. Neither API
key reaches a report, the log or the pipe.

The service starts the subprocess in its own process group, which the
processes it starts inherit (the engine's spawned subprocesses, the
resource tracker). As soon as the translation service that started it is
gone (its parent changes, or the pipe is closed), the subprocess ends the
other processes of its group (termination signal, a limited wait, forced
kill), then itself, and reports nothing after that. Whatever survives, for
example when the subprocess itself is killed, the service reaps by process
marker (``zotero_translate.processes``).

Test only: ``ZT_TEST_WORKER_SPAWN_CHILD=1`` makes the subprocess start a
long-running child the way the engine does (``multiprocessing``, spawn);
``ZT_TEST_WORKER_BAD_REPORTS=1``, see ``BAD_REPORTS``.
``ZT_TEST_WORKER_HANG_AT_EXIT=1``, see ``HANG_AT_EXIT``.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import multiprocessing
import os
import threading
import time
import traceback
from pathlib import Path

from zotero_translate import processes
from zotero_translate import task_dir as td
from zotero_translate.paper_task import PaperTaskError
from zotero_translate.paper_task import run_paper_task
from zotero_translate.settings import SettingsError
from zotero_translate.settings import load_pipeline_settings

# Test only: when set, the subprocess first sends a line that is not JSON and
# a progress report without a stage, which the service must drop.
BAD_REPORTS = "ZT_TEST_WORKER_BAD_REPORTS"
# Test only: when set, the subprocess starts a child that sleeps for an
# hour, by multiprocessing's spawn method like the engine's subprocesses.
SPAWN_CHILD = "ZT_TEST_WORKER_SPAWN_CHILD"
# Test only: after its result, the worker registers a C exit handler that
# never returns, as a native library's teardown once did (M3 ticket 13).
HANG_AT_EXIT = "ZT_TEST_WORKER_HANG_AT_EXIT"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zotero_translate.worker")
    parser.add_argument("task_dir", type=Path)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--report-fd", type=int, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--argon-env-file", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--cache-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    directory: Path = args.task_dir
    parent = os.getppid()
    threading.Thread(target=_exit_when_orphaned, args=(parent,), daemon=True).start()
    os.set_inheritable(args.report_fd, False)  # its own children must not hold the pipe
    pipe = os.fdopen(args.report_fd, "w", encoding="utf-8")
    pipe_lock = threading.Lock()

    def send(type: str, **fields) -> None:
        _exit_if_orphaned(parent)
        line = json.dumps({"attempt": args.attempt, "type": type, **fields}, ensure_ascii=False)
        with pipe_lock:
            try:
                pipe.write(line + "\n")
                pipe.flush()
            except OSError:  # the service is gone
                _exit_orphaned()

    if os.environ.get(SPAWN_CHILD):  # test only: a child like the engine's
        # Daemonic: a normal exit of this process ends it; only a killed
        # subprocess leaves it behind.
        multiprocessing.get_context("spawn").Process(target=_sleep_for_an_hour, daemon=True).start()

    if os.environ.get(BAD_REPORTS):  # test only: two reports the service must drop
        with pipe_lock:
            pipe.write("not a report\n")
            pipe.write(json.dumps({"attempt": args.attempt, "type": "progress"}) + "\n")
            pipe.flush()

    report_path = td.attempt_report_path(directory, args.attempt)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        settings = load_pipeline_settings(
            args.env_file, args.argon_env_file, args.concurrency, args.cache_dir
        )
    except SettingsError as e:
        send("result", outcome="failed", error=str(e), category="permanent")
        return 1
    try:
        result = run_paper_task(
            directory / td.SOURCE_PDF,
            directory,
            settings,
            on_progress=lambda stage, progress: send("progress", stage=stage, progress=progress),
            report_path=report_path,
            attempt=args.attempt,
        )
    except PaperTaskError as e:
        send("result", outcome="failed", error=str(e), category=e.category)
        return 1
    except Exception as e:  # noqa: BLE001 - every failure must reach the service
        traceback.print_exc()
        send(
            "result",
            outcome="failed",
            error=f"论文任务出错：{type(e).__name__}: {e}",
            category="retryable",
        )
        return 1
    logging.info(
        "paper task finished in %.1fs; model requests=%d, max in flight: flash=%d, argon=%d; "
        "forwarded to argon=%d",
        time.monotonic() - started,
        result.model_requests,
        result.max_in_flight,
        result.argon_max_in_flight,
        result.forwarded_requests,
    )
    send(
        "result",
        outcome="succeeded",
        kept_source_paragraphs=result.kept_source_paragraphs,
        kept_source_algorithms=result.kept_source_algorithms,
    )
    if os.environ.get(HANG_AT_EXIT):
        _hang_at_exit()
    return 0


def _exit_if_orphaned(parent: int) -> None:
    if os.getppid() != parent:
        _exit_orphaned()


_orphan_lock = threading.Lock()


def _exit_orphaned() -> None:
    """The service is gone: end the other processes of this group, then this one."""
    with _orphan_lock:  # once, whichever thread notices first
        if os.getpgid(0) == os.getpid():  # the leader of its own group, as the service starts it
            try:
                processes.end(processes.group_members())
            except Exception:  # noqa: BLE001 - exit anyway; the service reaps by marker
                traceback.print_exc()
        os._exit(3)


def _hang_at_exit() -> None:
    """Make the process hang after Python's own shutdown, in a C exit handler."""
    libc = ctypes.CDLL(None)
    libc.atexit(ctypes.cast(libc.pause, ctypes.c_void_p))


def _sleep_for_an_hour() -> None:
    time.sleep(3600)


def _exit_when_orphaned(parent: int) -> None:
    while True:
        _exit_if_orphaned(parent)
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
