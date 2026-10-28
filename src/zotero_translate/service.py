"""Translation service: a local HTTP service that runs paper tasks.

HTTP interface (the contract with the translation plugin):

- ``GET /health`` -> ``{"service": "zotero-translate", "version": ...}``
- ``POST /tasks`` (multipart: a ``file`` part with the PDF; an optional
  ``filename`` part with the original file name) -> ``201 {"task_id": ...}``
- ``GET /tasks/{task_id}`` -> state, stage, progress, queue position, error,
  kept source paragraphs and downloadable files
- ``GET /tasks/{task_id}/files/mono`` and ``.../files/dual`` -> PDF
- ``GET /tasks/{task_id}/files/terms`` -> the paper's term list as JSON
- Unknown task ids return 404.

The service listens on 127.0.0.1 only and has no authentication. Tasks run
first in, first out, one at a time; each paper task runs in its own
subprocess, which reports progress through the status file in its task
directory. Successful model responses are cached in ``<data_dir>/model-cache``
and shared by all tasks.

When the service starts, every task left queued or running by an earlier
service is marked as failed ("翻译服务重启，任务中断"); there is no resuming.
A task subprocess ends itself when its service is gone.
"""

from __future__ import annotations

import collections
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

from zotero_translate import __version__
from zotero_translate import task_dir as td
from zotero_translate.paper_task import FIRST_STAGE
from zotero_translate.settings import model_cache_dir

HOST = "127.0.0.1"
RESTART_ERROR = "翻译服务重启，任务中断"
_TASK_ID = re.compile(r"^[0-9a-f]{32}$")


class TaskQueue:
    """Task directories under ``<data_dir>/tasks`` plus a FIFO run queue."""

    def __init__(self, data_dir: Path, env_file: Path, argon_env_file: Path, concurrency: int):
        self.tasks_dir = Path(data_dir) / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = model_cache_dir(data_dir)
        self.env_file = Path(env_file)
        self.argon_env_file = Path(argon_env_file)
        self.concurrency = concurrency
        self._queue: collections.deque[str] = collections.deque()  # waiting tasks
        self._cond = threading.Condition()
        self._fail_unfinished_tasks()
        threading.Thread(target=self._run_forever, name="task-runner", daemon=True).start()

    def _fail_unfinished_tasks(self) -> None:
        for status_file in self.tasks_dir.glob(f"*/{td.STATUS_FILE}"):
            directory = status_file.parent
            try:
                state = td.read_status(directory)["state"]
            except (OSError, ValueError, KeyError):
                continue
            if state not in td.TERMINAL_STATES:
                td.write_status(directory, state="failed", error=RESTART_ERROR)

    def submit(self, pdf: bytes, filename: str) -> str:
        task_id = uuid.uuid4().hex
        directory = self.tasks_dir / task_id
        directory.mkdir()
        (directory / td.SOURCE_PDF).write_bytes(pdf)
        (directory / "task.json").write_text(
            json.dumps(
                {"task_id": task_id, "filename": filename, "submitted_at": time.time()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        td.write_status(
            directory,
            state="queued",
            stage=td.QUEUED_STAGE,
            progress=0,
            error=None,
            kept_source_paragraphs=0,
            kept_source_algorithms=0,
        )
        with self._cond:
            self._queue.append(task_id)
            self._cond.notify()
        return task_id

    def task_dir(self, task_id: str) -> Path | None:
        if not _TASK_ID.match(task_id):
            return None
        directory = self.tasks_dir / task_id
        return directory if (directory / td.STATUS_FILE).is_file() else None

    def status(self, task_id: str) -> dict | None:
        directory = self.task_dir(task_id)
        if directory is None:
            return None
        status = td.read_status(directory)
        with self._cond:
            position = (
                self._queue.index(task_id) + 1 if task_id in self._queue else None
            )
        return {
            "task_id": task_id,
            "state": status["state"],
            "stage": status["stage"],
            "progress": status["progress"],
            "queue_position": position,
            "error": status.get("error"),
            "kept_source_paragraphs": status.get("kept_source_paragraphs", 0),
            "kept_source_algorithms": status.get("kept_source_algorithms", 0),
            "files": td.available_files(directory),
        }

    def _run_forever(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                task_id = self._queue.popleft()
                td.write_status(
                    self.tasks_dir / task_id, state="running", stage=FIRST_STAGE, progress=0
                )
            try:
                self._run(task_id)
            except Exception as e:  # noqa: BLE001 - the runner thread must survive
                td.write_status(
                    self.tasks_dir / task_id, state="failed", error=f"无法启动论文任务：{e}"
                )

    def _run(self, task_id: str) -> None:
        directory = self.tasks_dir / task_id
        command = [
            sys.executable,
            "-m",
            "zotero_translate.worker",
            str(directory),
            "--env-file",
            str(self.env_file),
            "--argon-env-file",
            str(self.argon_env_file),
            "--concurrency",
            str(self.concurrency),
            "--cache-dir",
            str(self.cache_dir),
        ]
        with (directory / td.LOG_FILE).open("ab") as log:
            exit_code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode
        if td.read_status(directory)["state"] not in td.TERMINAL_STATES:
            td.write_status(
                directory, state="failed", error=f"论文任务进程异常退出（退出码 {exit_code}）"
            )


def make_handler(queue: TaskQueue) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parts = self.path.split("?", 1)[0].strip("/").split("/")
            if parts == ["health"]:
                return self._json(200, {"service": "zotero-translate", "version": __version__})
            if len(parts) == 2 and parts[0] == "tasks":
                status = queue.status(parts[1])
                if status is None:
                    return self._not_found()
                return self._json(200, status)
            if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "files":
                return self._send_file(parts[1], parts[3])
            return self._not_found()

        def do_POST(self):
            if self.path.split("?", 1)[0].rstrip("/") != "/tasks":
                return self._not_found()
            length = int(self.headers.get("Content-Length") or 0)
            fields = _parse_multipart(
                self.headers.get("Content-Type", ""), self.rfile.read(length)
            )
            pdf, part_filename = fields.get("file", (None, None))
            if not pdf or not pdf.startswith(b"%PDF"):
                return self._json(400, {"error": "请求中没有 PDF 文件"})
            filename = (fields.get("filename") or (b"", None))[0].decode("utf-8") or (
                part_filename or "paper.pdf"
            )
            return self._json(201, {"task_id": queue.submit(pdf, filename)})

        def _send_file(self, task_id: str, name: str):
            directory = queue.task_dir(task_id)
            file = td.DOWNLOAD_FILES.get(name)
            if directory is None or file is None or not (directory / file).is_file():
                return self._not_found()
            data = (directory / file).read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8" if file.endswith(".json") else "application/pdf",
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _not_found(self):
            self._json(404, {"error": "翻译服务中找不到该任务"})

        def _json(self, code: int, payload: dict):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _parse_multipart(content_type: str, body: bytes) -> dict[str, tuple[bytes, str | None]]:
    """Return ``{field name: (content, filename)}`` for a multipart/form-data body."""
    if not content_type.startswith("multipart/form-data"):
        return {}
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body
    )
    fields = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            fields[name] = (part.get_payload(decode=True) or b"", part.get_filename())
    return fields


def serve(
    data_dir: Path, env_file: Path, argon_env_file: Path, port: int, concurrency: int
) -> None:
    """Run the service until interrupted; print ``{"url": ...}`` once listening."""
    queue = TaskQueue(data_dir, env_file, argon_env_file, concurrency)
    server = ThreadingHTTPServer((HOST, port), make_handler(queue))
    server.daemon_threads = True
    print(json.dumps({"url": f"http://{HOST}:{server.server_address[1]}"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
