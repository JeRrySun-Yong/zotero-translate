"""Subprocess entry point: run one paper task in its task directory.

Usage: python -m zotero_translate.worker TASK_DIR --env-file FILE --concurrency N

Progress, the final state and any failure reason go to the task's status
file. The API key never reaches the status file or the log.
"""

from __future__ import annotations

import argparse
import logging
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
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    directory: Path = args.task_dir
    started = time.monotonic()
    try:
        settings = load_model_settings(args.env_file, args.concurrency)
        result = run_paper_task(
            directory / td.SOURCE_PDF,
            directory,
            settings,
            on_progress=lambda stage, progress: td.write_status(
                directory, stage=stage, progress=progress
            ),
        )
    except (SettingsError, PaperTaskError) as e:
        td.write_status(directory, state="failed", error=str(e))
        return 1
    except Exception as e:  # noqa: BLE001 - every failure must reach the status file
        traceback.print_exc()
        td.write_status(directory, state="failed", error=f"论文任务出错：{type(e).__name__}: {e}")
        return 1
    logging.info(
        "paper task finished in %.1fs; model requests=%d, max in flight=%d",
        time.monotonic() - started,
        result.model_requests,
        result.max_in_flight,
    )
    td.write_status(directory, state="succeeded", stage=td.DONE_STAGE, progress=100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
