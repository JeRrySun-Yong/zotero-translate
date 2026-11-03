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

The service listens on 127.0.0.1 only and has no authentication. One service
per data directory: it takes an exclusive lock on ``<data_dir>/service.lock``,
then binds its port, and only then restores the queue; when the lock or the
port is taken it exits with status 1 and a readable reason on stderr, and
touches no task.

Tasks run first in, first out, one at a time. The service alone writes the
task state: every transition goes through ``TaskQueue._decide``, under one
lock, and is saved with the counters it changes as one atomic replacement of
the status file. Each attempt runs in its own subprocess
(``zotero_translate.worker``), which reports progress and its result over a
pipe; the service adopts only reports of the task's current attempt while the
task is running, and drops any other. Before it starts the subprocess, the
service saves the attempt's process marker ``<task_id>:<attempt>`` in the
status file and passes it to the subprocess in the environment variable
``ZT_PROCESS_MARK``, which every process the subprocess starts inherits.

Each attempt writes its own task report, ``attempts/<n>/report.json``; when
an attempt ends, the service copies it to the task's ``report.json`` with a
summary of all attempts (number, start and end time, outcome, reason). The
service writes one line per task event to standard output (the service log):
submitted, started, succeeded, failed. No API key and no HTTP poll is logged.

When the service starts, every task left queued or running by an earlier
service is marked as failed ("翻译服务重启，任务中断"); there is no resuming.
The report of an attempt cut short so is marked incomplete. A task
subprocess ends itself when its service is gone.

Test-only settings (environment variables of the service; never set them in
production): ``ZT_TEST_PAUSE_AFTER_MARKER`` (after the process marker is saved,
before the subprocess starts) and ``ZT_TEST_PAUSE_BEFORE_ADOPT`` (after the
subprocess has exited, before its result is adopted) name a directory. At the
pause the service creates ``<directory>/<task_id>-<attempt>.paused`` and waits
until that file is deleted. ``ZT_TEST_WORKER_BAD_REPORTS`` makes the
subprocess send two malformed reports (``zotero_translate.worker``).

One task's error never stops the queue: a worker report the service cannot
use is dropped with a log line (event 丢弃上报) and the attempt goes on; an
error of the service itself while it carries an attempt through fails that
task with a readable reason, and the runner goes on with the next task.
"""

from __future__ import annotations

import collections
import datetime
import errno
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
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
LOCK_FILE = "service.lock"
MARK_ENV = "ZT_PROCESS_MARK"
PAUSE_AFTER_MARKER = "ZT_TEST_PAUSE_AFTER_MARKER"
PAUSE_BEFORE_ADOPT = "ZT_TEST_PAUSE_BEFORE_ADOPT"
_TASK_ID = re.compile(r"^[0-9a-f]{32}$")

# The state fields a new task starts with (spec section 1), besides state,
# stage, progress and the kept source counts.
FIRST_QUEUE_CLASS = "first"
_INITIAL_FIELDS = {
    "attempt": 0,  # attempts started, interrupted ones included
    "reruns_used": 0,
    "consecutive_interruptions": 0,
    "next_attempt_at": None,  # wall clock time (seconds since the epoch)
    "queue_class": FIRST_QUEUE_CLASS,
    "error": None,  # the reason of the last failure
    "failure_category": None,  # "permanent" or "retryable"
    "cancel_requested": False,
    "process_marker": None,  # "<task_id>:<attempt>" of the current attempt
    "cleanup_error": None,
    "attempts": [],  # {"attempt", "started_at", "ended_at", "outcome", "reason"}
}
_EVENTS = {
    "submitted": "提交",
    "started": "开始",
    "succeeded": "成功",
    "failed": "失败",
    "report_dropped": "丢弃上报",  # a worker report the service cannot use
}


class TaskQueue:
    """Task directories under ``<data_dir>/tasks`` plus a FIFO run queue.

    Construction touches no task; ``restore`` handles what an earlier
    service left, and ``start`` starts running tasks.
    """

    def __init__(self, data_dir: Path, env_file: Path, argon_env_file: Path, concurrency: int):
        self.tasks_dir = Path(data_dir) / "tasks"
        self.cache_dir = model_cache_dir(data_dir)
        self.env_file = Path(env_file)
        self.argon_env_file = Path(argon_env_file)
        self.concurrency = concurrency
        self._queue: collections.deque[str] = collections.deque()  # waiting tasks
        # The one lock every state transition and the queue go through.
        self._cond = threading.Condition(threading.RLock())

    def restore(self) -> None:
        """Fail every task an earlier service left queued or running."""
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        for status_file in sorted(self.tasks_dir.glob(f"*/{td.STATUS_FILE}")):
            task_id = status_file.parent.name
            try:
                state = td.read_status(status_file.parent)["state"]
            except (OSError, ValueError, KeyError):
                continue
            if state not in td.TERMINAL_STATES:
                self._decide(
                    task_id,
                    "failed",
                    {"state": "failed", "error": RESTART_ERROR, "failure_category": "retryable"},
                    outcome="interrupted" if state == "running" else None,
                    reason=RESTART_ERROR,
                )

    def start(self) -> None:
        threading.Thread(target=self._run_forever, name="task-runner", daemon=True).start()

    # -- the arbiter ---------------------------------------------------------

    def _decide(
        self,
        task_id: str,
        event: str,
        fields: dict,
        outcome: str | None = None,
        reason: str | None = None,
    ) -> dict:
        """Apply one state transition: save ``fields`` atomically and log ``event``.

        ``outcome`` (succeeded, failed, interrupted) ends the current attempt:
        its summary entry gets the end time, outcome and reason, and the
        task's ``report.json`` becomes that attempt's report with the summary
        of all attempts. The report is written before the status file, so a
        transition whose report cannot be written is not saved at all.
        """
        directory = self.tasks_dir / task_id
        with self._cond:
            status = td.read_status(directory)
            changes = dict(fields)
            if outcome is not None and status.get("attempts"):
                attempts = [dict(a) for a in status["attempts"]]
                attempts[-1].update(ended_at=time.time(), outcome=outcome, reason=reason)
                changes["attempts"] = attempts
                self._write_task_report(directory, {**status, **changes}, outcome, reason)
            status = td.write_status(directory, **changes)
            self._log(task_id, event, status.get("attempt", 0), reason)
            return status

    def _adopt(self, task_id: str, attempt: int, event: str, fields: dict, **end) -> bool:
        """Apply a worker's report if it is of the running current attempt; else drop it."""
        with self._cond:
            status = td.read_status(self.tasks_dir / task_id)
            current = (
                status["state"] == "running"
                and status.get("attempt") == attempt
                and not status.get("cancel_requested")
            )
            if current:
                if event == "progress":
                    td.write_status(self.tasks_dir / task_id, **fields)
                else:
                    self._decide(task_id, event, fields, **end)
            return current

    def _write_task_report(self, directory: Path, status: dict, outcome: str, reason) -> None:
        attempt = status["attempt"]
        path = td.attempt_report_path(directory, attempt)
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # No report from the worker (it could not read its settings or
            # start, or was cut short): only what the outcome says.
            report = {
                "attempt": attempt,
                "state": "failed" if outcome == "failed" else None,
                "error": reason,
            }
        if outcome == "interrupted":
            # The attempt did not run to its end: say so, invent nothing.
            report.update(complete=False, incomplete_reason=reason)
            path.parent.mkdir(parents=True, exist_ok=True)
            td.write_json(path, report, indent=2)
        report["attempts"] = status["attempts"]
        td.write_json(directory / td.REPORT_FILE, report, indent=2)

    def _log(self, task_id: str, event: str, attempt: int, reason: str | None) -> None:
        try:
            filename = json.loads((self.tasks_dir / task_id / "task.json").read_text("utf-8"))[
                "filename"
            ]
        except (OSError, ValueError, KeyError):
            filename = None
        line = (
            f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}"
            f" task={task_id} attempt={attempt} event={_EVENTS[event]}"
            f" file={json.dumps(filename, ensure_ascii=False)}"
        )
        if reason:
            line += f" reason={json.dumps(reason, ensure_ascii=False)}"
        try:
            print(line, flush=True)
        except (OSError, ValueError):  # no standard output: the transition stands
            pass

    # -- HTTP side -----------------------------------------------------------

    def submit(self, pdf: bytes, filename: str) -> str:
        task_id = uuid.uuid4().hex
        directory = self.tasks_dir / task_id
        directory.mkdir()
        (directory / td.SOURCE_PDF).write_bytes(pdf)
        submitted_at = time.time()
        (directory / "task.json").write_text(
            json.dumps(
                {"task_id": task_id, "filename": filename, "submitted_at": submitted_at},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self._cond:
            td.write_status(
                directory,
                state="queued",
                stage=td.QUEUED_STAGE,
                progress=0,
                kept_source_paragraphs=0,
                kept_source_algorithms=0,
                **_INITIAL_FIELDS,
                queue_key=submitted_at,
            )
            self._log(task_id, "submitted", 0, None)
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
        with self._cond:
            status = td.read_status(directory)
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

    # -- the runner ----------------------------------------------------------

    def _run_forever(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                task_id = self._queue.popleft()
            try:
                self._run_attempt(task_id)
            except Exception as e:  # noqa: BLE001 - one task's error must not stop the queue
                self._fail_after_error(task_id, e)

    def _fail_after_error(self, task_id: str, error: Exception) -> None:
        """Fail a task whose attempt the service itself could not carry through.

        Best effort and never raises: the runner goes on with the next task.
        The task's report.json is written if it can be.
        """
        reason = f"翻译服务处理任务出错：{type(error).__name__}: {error}"
        print(f"task={task_id}: {reason}", file=sys.stderr, flush=True)
        traceback.print_exception(error, file=sys.stderr)
        directory = self.tasks_dir / task_id
        with self._cond:
            try:
                status = td.read_status(directory)
                if status["state"] in td.TERMINAL_STATES:
                    return
                changes = {"state": "failed", "error": reason, "failure_category": "retryable"}
                if status.get("attempts"):
                    attempts = [dict(a) for a in status["attempts"]]
                    attempts[-1].update(ended_at=time.time(), outcome="failed", reason=reason)
                    changes["attempts"] = attempts
                    try:
                        self._write_task_report(
                            directory, {**status, **changes}, "failed", reason
                        )
                    except Exception:  # noqa: BLE001 - the state matters more than the report
                        traceback.print_exc()
                td.write_status(directory, **changes)
                self._log(task_id, "failed", status.get("attempt", 0), reason)
            except Exception:  # noqa: BLE001 - nothing more can be done for this task
                traceback.print_exc()

    def _run_attempt(self, task_id: str) -> None:
        """Start the task's next attempt, run it, and adopt its result."""
        with self._cond:
            status = td.read_status(self.tasks_dir / task_id)
            attempt = status.get("attempt", 0) + 1
            self._decide(
                task_id,
                "started",
                {
                    "state": "running",
                    "stage": FIRST_STAGE,
                    "progress": 0,
                    "attempt": attempt,
                    "process_marker": f"{task_id}:{attempt}",
                    "attempts": status.get("attempts", [])
                    + [
                        {
                            "attempt": attempt,
                            "started_at": time.time(),
                            "ended_at": None,
                            "outcome": None,
                            "reason": None,
                        }
                    ],
                },
            )
        try:
            result = self._run(task_id, attempt)
        except Exception as e:  # noqa: BLE001 - reported as this attempt's failure
            result = _failure(f"无法启动论文任务：{e}", "retryable")
        _test_pause(PAUSE_BEFORE_ADOPT, task_id, attempt)
        if result["outcome"] == "succeeded":
            self._adopt(
                task_id,
                attempt,
                "succeeded",
                {
                    "state": "succeeded",
                    "stage": td.DONE_STAGE,
                    "progress": 100,
                    "error": None,
                    "failure_category": None,
                    "consecutive_interruptions": 0,
                    "kept_source_paragraphs": result.get("kept_source_paragraphs", 0),
                    "kept_source_algorithms": result.get("kept_source_algorithms", 0),
                },
                outcome="succeeded",
            )
        else:
            self._adopt(
                task_id,
                attempt,
                "failed",
                {
                    "state": "failed",
                    "error": result["error"],
                    "failure_category": result["category"],
                    "consecutive_interruptions": 0,
                },
                outcome="failed",
                reason=result["error"],
            )

    def _run(self, task_id: str, attempt: int) -> dict:
        """Run one attempt's subprocess until it exits; return its result report."""
        directory = self.tasks_dir / task_id
        marker = td.read_status(directory)["process_marker"]
        _test_pause(PAUSE_AFTER_MARKER, task_id, attempt)
        read_end, write_end = os.pipe()
        command = [
            sys.executable,
            "-m",
            "zotero_translate.worker",
            str(directory),
            "--attempt",
            str(attempt),
            "--report-fd",
            str(write_end),
            "--env-file",
            str(self.env_file),
            "--argon-env-file",
            str(self.argon_env_file),
            "--concurrency",
            str(self.concurrency),
            "--cache-dir",
            str(self.cache_dir),
        ]
        try:
            with (directory / td.LOG_FILE).open("ab") as log:
                process = subprocess.Popen(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    pass_fds=(write_end,),
                    env={**os.environ, MARK_ENV: marker},
                )
        except BaseException:
            os.close(read_end)
            raise
        finally:
            os.close(write_end)
        result = None
        with os.fdopen(read_end, encoding="utf-8", errors="replace") as reports:
            for line in reports:
                # A report the service cannot use is dropped and logged; it
                # never ends the attempt while the subprocess still runs.
                try:
                    result = self._take_report(task_id, attempt, line)
                except Exception as e:  # noqa: BLE001
                    self._log(
                        task_id,
                        "report_dropped",
                        attempt,
                        f"{type(e).__name__}: {e}；上报内容：{line.strip()[:200]}",
                    )
                if result is not None:
                    break
        exit_code = process.wait()
        if result is None:
            return _failure(f"论文任务进程异常退出（退出码 {exit_code}）", "retryable")
        return result


    def _take_report(self, task_id: str, attempt: int, line: str) -> dict | None:
        """Adopt a progress report, or return a valid result of this attempt.

        Raise ``ValueError``, ``KeyError`` or ``TypeError`` for a report that
        is malformed or not of this attempt.
        """
        report = json.loads(line)
        if report["attempt"] != attempt:
            raise ValueError(f"不是本次尝试（第 {attempt} 次）的上报")
        if report["type"] == "progress":
            self._adopt(
                task_id,
                attempt,
                "progress",
                {"stage": str(report["stage"]), "progress": int(report["progress"])},
            )
            return None
        if report["type"] != "result":
            raise ValueError(f"未知的上报类型 {report['type']!r}")
        if report["outcome"] == "failed":
            if not isinstance(report["error"], str) or report["category"] not in (
                "permanent",
                "retryable",
            ):
                raise ValueError("失败结果缺少原因或类别")
        elif report["outcome"] != "succeeded":
            raise ValueError(f"未知的结局 {report['outcome']!r}")
        return report


def _failure(error: str, category: str) -> dict:
    return {"outcome": "failed", "error": error, "category": category}


def _test_pause(setting: str, task_id: str, attempt: int) -> None:
    """Test only: wait at a pause point while ``<dir>/<task_id>-<attempt>.paused`` exists."""
    directory = os.environ.get(setting)
    if not directory:
        return
    flag = Path(directory) / f"{task_id}-{attempt}.paused"
    flag.write_text(setting, encoding="utf-8")
    while flag.exists():
        time.sleep(0.05)


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


def _lock_data_dir(data_dir: Path):
    """Take the data directory's exclusive lock; return its open file, or None if taken.

    The lock lasts as long as the file stays open, i.e. until the service
    exits, however it exits. Task subprocesses do not inherit it.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = open(data_dir / LOCK_FILE, "a")  # noqa: SIM115 - held for the process's life
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def serve(
    data_dir: Path, env_file: Path, argon_env_file: Path, port: int, concurrency: int
) -> int:
    """Run the service until interrupted; print ``{"url": ...}`` once listening.

    Return 1 at once, touching no task, when another service holds the data
    directory or the port is taken.
    """
    data_dir = Path(data_dir)
    lock = _lock_data_dir(data_dir)
    if lock is None:
        print(
            f"已有翻译服务在使用数据目录 {data_dir}（{data_dir / LOCK_FILE} 被锁定），本服务退出",
            file=sys.stderr,
        )
        return 1
    queue = TaskQueue(data_dir, env_file, argon_env_file, concurrency)
    try:
        server = ThreadingHTTPServer((HOST, port), make_handler(queue))
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        print(f"端口 {port} 已被占用，本服务退出", file=sys.stderr)
        return 1
    server.daemon_threads = True
    print(json.dumps({"url": f"http://{HOST}:{server.server_address[1]}"}), flush=True)
    queue.restore()
    queue.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        lock.close()
    return 0
