"""Layout of one task directory and its status file.

The task directory holds the source PDF, the status file, the two output
PDFs, the task report and the log. The paper task subprocess writes the status file; the
translation service reads it to answer status queries.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SOURCE_PDF = "source.pdf"
STATUS_FILE = "status.json"
LOG_FILE = "task.log"
REPORT_FILE = "report.json"
OUTPUT_FILES = {"mono": "mono.pdf", "dual": "dual.pdf"}

QUEUED_STAGE = "排队中"
DONE_STAGE = "完成"
TERMINAL_STATES = {"succeeded", "failed"}


def read_status(task_dir: Path) -> dict:
    return json.loads((Path(task_dir) / STATUS_FILE).read_text(encoding="utf-8"))


def write_status(task_dir: Path, **fields) -> dict:
    """Merge ``fields`` into the status file; replace the file atomically."""
    task_dir = Path(task_dir)
    path = task_dir / STATUS_FILE
    status = read_status(task_dir) if path.exists() else {}
    status.update(fields)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return status


def available_files(task_dir: Path) -> list[str]:
    return [name for name, file in OUTPUT_FILES.items() if (Path(task_dir) / file).is_file()]
