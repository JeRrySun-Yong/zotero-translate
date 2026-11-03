"""Layout of one task directory and its status file.

The task directory holds the source PDF, the status file, the two output
PDFs, the paper dossier, the paper's term list, the task report, the log,
and ``attempts/<n>/report.json``, the task report of each attempt. Only the
translation service writes the status file (``zotero_translate.service``);
the paper task subprocess reports its progress and result to the service.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SOURCE_PDF = "source.pdf"
STATUS_FILE = "status.json"
LOG_FILE = "task.log"
REPORT_FILE = "report.json"
DOSSIER_FILE = "dossier.json"
TERMS_FILE = "terms.json"
ATTEMPTS_DIR = "attempts"
OUTPUT_FILES = {"mono": "mono.pdf", "dual": "dual.pdf"}
# Files that ``GET /tasks/{task_id}/files/{name}`` serves, by name.
DOWNLOAD_FILES = {**OUTPUT_FILES, "terms": TERMS_FILE}

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
    write_json(path, status)
    return status


def write_json(path: Path, data: dict, indent: int | None = None) -> None:
    """Write ``data`` to ``path`` as JSON, replacing the file atomically."""
    path = Path(path)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def attempt_report_path(task_dir: Path, attempt: int) -> Path:
    """The task report of one attempt (attempts count from 1)."""
    return Path(task_dir) / ATTEMPTS_DIR / str(attempt) / REPORT_FILE


def available_files(task_dir: Path) -> list[str]:
    return [name for name, file in DOWNLOAD_FILES.items() if (Path(task_dir) / file).is_file()]
