"""Subprocess entry point: run one paper task in its task directory.

Usage: python -m zotero_translate.worker TASK_DIR --env-file FILE --concurrency N
[--cache-dir DIR]

Progress, the final state and any failure reason go to the task's status
file. The API key never reaches the status file or the log.

The subprocess ends itself as soon as the translation service that started
it is gone, and never writes the status file after that: a restarted service
marks the task as failed, and a leftover subprocess must not overwrite that.
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
import traceback
from pathlib import Path

from zotero_translate import task_dir as td
from zotero_translate.paper_task import PaperTaskError
from zotero_translate.paper_task import run_paper_task
from zotero_translate.settings import SettingsError
from zotero_translate.settings import load_model_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zotero_translate.worker")
    parser.add_argument("task_dir", type=Path)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--cache-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    directory: Path = args.task_dir
    parent = os.getppid()
    threading.Thread(target=_exit_when_orphaned, args=(parent,), daemon=True).start()

    def write_status(**fields) -> None:
        _exit_if_orphaned(parent)
        td.write_status(directory, **fields)

    started = time.monotonic()
    try:
        settings = load_model_settings(args.env_file, args.concurrency, args.cache_dir)
        result = run_paper_task(
            directory / td.SOURCE_PDF,
            directory,
            settings,
            on_progress=lambda stage, progress: write_status(stage=stage, progress=progress),
        )
    except (SettingsError, PaperTaskError) as e:
        write_status(state="failed", error=str(e))
        return 1
    except Exception as e:  # noqa: BLE001 - every failure must reach the status file
        traceback.print_exc()
        write_status(state="failed", error=f"论文任务出错：{type(e).__name__}: {e}")
        return 1
    logging.info(
        "paper task finished in %.1fs; model requests=%d, max in flight=%d",
        time.monotonic() - started,
        result.model_requests,
        result.max_in_flight,
    )
    write_status(
        state="succeeded",
        stage=td.DONE_STAGE,
        progress=100,
        kept_source_paragraphs=result.kept_source_paragraphs,
        kept_source_algorithms=result.kept_source_algorithms,
    )
    return 0


def _exit_if_orphaned(parent: int) -> None:
    if os.getppid() != parent:
        os._exit(3)


def _exit_when_orphaned(parent: int) -> None:
    while True:
        _exit_if_orphaned(parent)
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
