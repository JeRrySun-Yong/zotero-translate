"""Translation service: a local HTTP service that runs paper tasks.

HTTP interface (the contract with the translation plugin):

- ``GET /health`` -> ``{"service": "zotero-translate", "version": ...}``
- ``POST /tasks`` (multipart: a ``file`` part with the PDF; an optional
  ``filename`` part with the original file name; an optional ``profile``
  part with a model profile id) -> ``201 {"task_id": ...}``. The task
  records the profile named, else (no part, or an empty one) the current
  profile. A named profile must exist (else 422) and, unless it is
  ``gemini``, which always can be named, have passed its connection test
  (else 409); a refused submission leaves no task. The plugin's retry,
  which submits the paper again, names the failed task's profile, so a
  retry keeps it whatever profile is current by then.
- ``GET /tasks/{task_id}`` -> state (queued, running, waiting_rerun,
  succeeded, failed, cancelled), stage, progress, attempt (attempts started,
  interrupted ones included; 0 until the first starts), reruns_used and
  reruns_max (2), next_attempt_in_seconds (only while waiting_rerun), queue
  position (only while queued), error (the last failure's reason, only
  while waiting_rerun or failed), cancel_requested (the service accepted a
  cancel and is reaping the task's processes), cleanup_error (why the task's processes could not be reaped, or
  null), kept source paragraphs and downloadable files
- ``POST /tasks/{task_id}/cancel`` -> the same object as the query: 200 when
  the task is cancelled (a queued, running or waiting task once its
  processes are reaped, or one already cancelled), 503 when its processes
  could not be reaped (cleanup_error says why), 409 when it has succeeded or
  failed, unchanged
- ``GET /tasks/{task_id}/files/mono`` and ``.../files/dual`` -> PDF
- ``GET /tasks/{task_id}/files/terms`` -> the paper's term list as JSON
- Unknown task ids return 404.
- The task query also gives ``profile``, ``{"id", "name"}``: the model
  profile the task recorded when it was submitted.

Model profiles (M4 ticket 02, ADR 0005; ``zotero_translate.profiles``). A
profile object: ``id`` (``"gemini"`` for the built-in one, else 12 hex
digits), ``name``, ``builtin``, ``current`` (bool), ``tested`` (its last
connection test succeeded, for its present settings; always true for
Gemini), ``base_url``, ``model``; a user profile also has ``concurrency``,
``request_timeout_seconds`` (null: the default 480), ``stream`` (bool:
streamed requests, M4 ticket 12; ``ZT_STREAM=1`` in its file), ``api_key`` (``{"set":
true, "last4": "abcd"}``, never the key) and ``last_test`` (null, or
``{"result", "message", "seconds", "http_status", "tested_at"}``); Gemini
has ``roles``, ``{"flash"|"argon": {"base_url", "model", "concurrency"}}``.

- ``GET /profiles`` -> ``{"current": id, "profiles": [Gemini, then the user
  profiles by name]}``; ``GET /profiles/{id}`` -> one profile object.
- ``POST /profiles`` ``{"name", "base_url", "api_key", "model",
  "concurrency"?, "request_timeout_seconds"?, "stream"?}`` -> ``201`` the new profile
  (concurrency 1-256, default 16; timeout seconds up to 3600, or null;
  stream true or false, default false).
- ``PUT /profiles/{id}`` with any of those fields -> ``200`` the profile; a
  field left out keeps its value (so does the key). A new address, key,
  model, timeout or stream setting makes it untested; if it was current, Gemini is current
  again (event 当前配置改回 Gemini).
- ``DELETE /profiles/{id}`` ``{}`` -> ``200 {"deleted": id, "current": id}``;
  409 for Gemini and while an unfinished task recorded the profile.
- ``POST /profiles/{id}/test`` ``{}`` -> ``200 {"ok", "result", "message",
  "seconds", "http_status"}`` once the test has run (it takes up to 60
  seconds), whatever its outcome; ``result`` is one of succeeded,
  invalid_address (an address in the file, edited outside the service,
  that cannot be one; the service refuses such an address with 400 when a
  profile is saved), unreachable, unauthorized (HTTP 401, 403), model_not_found (HTTP 404, or a
  400 or 422 that names the model), timeout, unparseable, http_error (e.g.
  429, 5xx). One request with the paper task's model, reasoning effort,
  temperature and stream setting (a stream that breaks off is
  unparseable), which must answer the JSON object ``{"ok": true}``
  (``model_client.check_connection``). 409 for Gemini.
- ``POST /profiles/{id}/apply`` ``{}`` -> ``200 {"current": id, "name"}``:
  the tasks submitted from now on use it; 409 unless it is tested. Gemini
  can always be applied.
- Unknown profile ids return 404; a field that is missing, unknown or
  invalid, 400 with ``error``. Every error answer is ``{"error": reason}``.

Every profile request must name this service in its ``Host`` header
(``127.0.0.1:<port>`` or ``localhost:<port>``), else 403: a page whose host
name resolves to 127.0.0.1 cannot reach them. Every write (POST, PUT,
DELETE) also needs the header ``X-Zotero-Translate: 1`` (else 403) and a
JSON object body with ``Content-Type: application/json`` (else 415, or 400
for a body that is not a JSON object): a web page cannot send such a
request across origins without a CORS preflight, and the service answers
every ``OPTIONS`` request with 403 and no ``Access-Control-*`` header.

A task records the current profile when it is submitted (``task.json``:
``profile_id``, ``profile_name``) and every attempt uses it, a requeue and
an automatic rerun included: Gemini's attempts get the start options, a
user profile's the profile's file (``--profile-file``), as it is when the
attempt starts. So an edit of a profile (e.g. a new key or model) reaches
the later attempts of tasks that recorded it, queued ones included, though
the task report of each attempt names the model it used. When a profile's
file is gone (deleted outside the service; the service refuses to delete a
profile an unfinished task recorded), the next attempt of a task that
recorded it fails at once with a settings error (``permanent``, reason
找不到模型配置的文件…): no automatic rerun, and no fallback to another
profile; an attempt already running goes on with the settings it read.
Both roles of a user profile share its one endpoint and
budget, with no hedging (``model_client.PipelineModels``). The current
profile survives a restart (``<data_dir>/model-profiles.json``); when its
file is gone, the current profile is Gemini again, with a log line. Profile
events are logged as ``profile=<id> event=... name=... reason=...``:
created (新建配置), edited (修改配置), deleted (删除配置), tested (测试连接,
the outcome as reason), applied (应用配置), back to Gemini (当前配置改回
Gemini). The profiles' files are in ``--profiles-dir``, by default
``~/.config/zotero-translate/profiles`` (``ZT_PROFILES_DIR``).

The service listens on 127.0.0.1 only and has no authentication. One service
per data directory: it takes an exclusive lock on ``<data_dir>/service.lock``,
then binds its port, and only then restores the queue; when the lock or the
port is taken it exits with status 1 and a readable reason on stderr, and
touches no task.

Tasks run one at a time, ordered by queue class (tasks interrupted by a
restart first, by submission time; then due automatic reruns, by due time,
then submission time; then first attempts, by submission time); class and
sort key are saved, so the order survives a restart.

An attempt that fails with category ``retryable`` (anything but a
credential error or unreadable settings; also a subprocess that exits
without a result) puts the task in ``waiting_rerun`` with a saved due time,
60 seconds later before the first rerun and 300 before the second (event
等待自动重跑). When it is due, the task is queued as a rerun and the rerun
counted, in one transition (event 自动重跑开始); it does not preempt the
running task, and no first attempt starts while a due rerun is not yet
queued. After 2 reruns, or on a ``permanent`` failure, the task fails with
that attempt's reason. A restart keeps a waiting task's due time and counts
no rerun again; an interrupted rerun attempt is requeued as interrupted,
using no rerun.

The service alone writes the task state: every transition goes through
``TaskQueue._decide``, under one lock, and is saved with the counters it changes as one atomic replacement of
the status file. Each attempt runs in its own subprocess
(``zotero_translate.worker``), which reports progress and its result over a
pipe; the service adopts only reports of the task's current attempt while the
task is running, and drops any other. Before it starts the subprocess, the
service saves the attempt's process marker ``<task_id>:<attempt>`` in the
status file and passes it to the subprocess in the environment variable
``ZT_PROCESS_MARK``, which every process the subprocess starts inherits. The
subprocess runs in a process group of its own. Before a new attempt starts,
when the service starts, and after a subprocess exits without a result, the
service reaps every process with a marker of the task
(``zotero_translate.processes``); if that cannot be confirmed, the task
keeps its state, the reason goes to its ``cleanup_error`` and the log (event
进程回收出错), and no new attempt starts. The service tries such a reap
again every 30 seconds (and at once on a repeated cancel); once it
succeeds, the task makes the transition it was waiting for (e.g. a
subprocess that exited without a result fails the attempt only then), and
when no reap has failed, scheduling resumes.

Each attempt writes its own task report, ``attempts/<n>/report.json``; when
an attempt ends, the service copies it to the task's ``report.json`` with a
summary of all attempts (number, start and end time, outcome, reason). An
attempt's request records are appended to ``attempts/<n>/requests.jsonl`` as
they happen; when an attempt ends, the service sets its report's ``requests``
to that file's records, which include the sends that ended after the worker
wrote its report (``zotero_translate.request_log``). The service writes
one line per task event to standard output (the service log):
submitted, started, interrupted, requeued, waiting for a rerun, rerun
started, cancel accepted, cancelled, succeeded, failed, cleanup error,
report dropped; and one line per model profile event (above). No API key
and no HTTP poll is logged.

A cancel is saved as accepted (event 接受取消); then the task's processes
are reaped by marker, and only after all have exited does the task become
``cancelled`` (event 已取消); a running task's attempt ends as cancelled,
its report marked incomplete. A task with an accepted cancel is never
started: not when its rerun comes due, not when it reaches the head of the
queue, not when its marker is saved but its subprocess not yet started, and
not after a restart, which reaps and completes the cancel, counting no
interruption. Its subprocess's reports, a late result included, are dropped.

When the service starts (``TaskQueue.restore``), a task an earlier service
left running was interrupted (reason "翻译服务重启，任务中断"; the attempt's
report is marked incomplete): it is queued again ahead of the others and
starts a new attempt, unless that was its third interruption in a row, which
fails it ("翻译服务多次重启，任务中断"). A queued task keeps its place. A task
directory from before M3 keeps its final state; queued, it is queued; running,
it fails ("升级前未完成的任务，请重试"). An error with one task fails that task
alone. A task subprocess ends its process group and itself when its service
is gone.

Test-only settings (environment variables of the service; never set them in
production): ``ZT_TEST_PAUSE_AFTER_MARKER`` (after the process marker is saved,
before the subprocess starts) and ``ZT_TEST_PAUSE_BEFORE_ADOPT`` (after the
subprocess has exited, before its result is adopted) name a directory. At the
pause the service creates ``<directory>/<task_id>-<attempt>.paused`` and waits
until that file is deleted. ``ZT_TEST_PAUSE_BEFORE_RERUN_QUEUE`` is a pause
of the same kind when a rerun is due, before it is queued (the attempt in
the file name is the one that failed), and ``ZT_TEST_PAUSE_BEFORE_REAP``
when the queue has picked the next task, before its earlier processes are
reaped (the attempt is the one about to start), and
``ZT_TEST_PAUSE_AFTER_CANCEL_ACCEPTED`` when a cancel has been saved as
accepted, before the task's processes are reaped (the attempt is the task's
current one, 0 before the first). ``ZT_TEST_REAP_SCAN_ERROR`` names a file:
while it exists, scanning for a task's processes fails, so reaping fails
(``zotero_translate.processes``). ``ZT_TEST_RERUN_WAITS_SECONDS``
("<first>,<second>") replaces the 60 and 300 seconds before the reruns.
``ZT_TEST_WORKER_BAD_REPORTS`` makes the
subprocess send two malformed reports, and ``ZT_TEST_WORKER_SPAWN_CHILD``
makes it start a child that sleeps for an hour, by multiprocessing's spawn
method like the engine (``zotero_translate.worker``).
``ZT_TEST_DOWNLOAD_DELAY_SECONDS`` ("<file>=<seconds>[,...]", e.g.
"dual=30") waits that long before sending that file of any task.
``ZT_TEST_HEDGE_AFTER_SECONDS`` (read by each worker, which inherits it;
``zotero_translate.settings``) replaces the 400-second hedging threshold.

One task's error never stops the queue: a worker report the service cannot
use is dropped with a log line (event 丢弃上报) and the attempt goes on; an
error of the service itself while an attempt runs ends that attempt as a
retryable failure (an automatic rerun while reruns remain), an error at any
other point fails the task, both with a readable reason, and the runner goes
on with the next task.
"""

from __future__ import annotations

import datetime
import errno
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

from zotero_translate import __version__
from zotero_translate import processes
from zotero_translate import task_dir as td
from zotero_translate.paper_task import FIRST_STAGE
from zotero_translate.request_log import REQUESTS_FILE
from zotero_translate.model_client import check_connection
from zotero_translate.profiles import BUILTIN_ID
from zotero_translate.profiles import BUILTIN_NAME
from zotero_translate.profiles import ProfileError
from zotero_translate.profiles import ProfileStore
from zotero_translate.request_log import load_records
from zotero_translate.settings import DEFAULT_PROFILES_DIR
from zotero_translate.settings import model_cache_dir

HOST = "127.0.0.1"
RESTART_ERROR = "翻译服务重启，任务中断"
MANY_RESTARTS_ERROR = "翻译服务多次重启，任务中断"
UPGRADE_ERROR = "升级前未完成的任务，请重试"
MAX_INTERRUPTIONS = 3  # in a row; the third fails the task
RERUNS_MAX = 2  # automatic reruns after the first attempt
RERUN_WAITS_SECONDS = (60.0, 300.0)  # before the first and the second rerun
REAP_RETRY_SECONDS = 30.0  # between tries of a reap that failed
CANCEL_REASON = "用户取消了任务"
LOCK_FILE = "service.lock"
WRITE_HEADER = "X-Zotero-Translate"  # the custom header of the profile writes; value "1"
PAUSE_AFTER_MARKER = "ZT_TEST_PAUSE_AFTER_MARKER"
PAUSE_BEFORE_ADOPT = "ZT_TEST_PAUSE_BEFORE_ADOPT"
PAUSE_BEFORE_RERUN_QUEUE = "ZT_TEST_PAUSE_BEFORE_RERUN_QUEUE"
PAUSE_BEFORE_REAP = "ZT_TEST_PAUSE_BEFORE_REAP"
PAUSE_AFTER_CANCEL_ACCEPTED = "ZT_TEST_PAUSE_AFTER_CANCEL_ACCEPTED"
RERUN_WAITS_SETTING = "ZT_TEST_RERUN_WAITS_SECONDS"  # "<first>,<second>", replaces RERUN_WAITS_SECONDS
DOWNLOAD_DELAY_SETTING = "ZT_TEST_DOWNLOAD_DELAY_SECONDS"  # "<file>=<seconds>[,...]"
_TASK_ID = re.compile(r"^[0-9a-f]{32}$")

# Queue classes (spec section 1, "排队顺序"), in the order they run: tasks
# interrupted by a restart, by submission time; due automatic reruns, by due
# time, then submission time; first attempts, by submission time. The sort
# key is the submission time, or [due time, submission time] for a rerun.
INTERRUPTED_QUEUE_CLASS = "interrupted"
RERUN_QUEUE_CLASS = "rerun"
FIRST_QUEUE_CLASS = "first"
_QUEUE_RANK = {INTERRUPTED_QUEUE_CLASS: 0, RERUN_QUEUE_CLASS: 1, FIRST_QUEUE_CLASS: 2}

# The state fields a new task starts with (spec section 1), besides state,
# stage, progress and the kept source counts.
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
    "interrupted": "中断",  # the attempt was cut short by a restart
    "requeued": "重新排队并开始新尝试",
    "waiting_rerun": "等待自动重跑",  # the attempt failed, retryable; a rerun is due later
    "rerun_started": "自动重跑开始",  # the rerun is due and queued
    "cancel_accepted": "接受取消",  # saved first: the task never runs again
    "cancelled": "已取消",
    "succeeded": "成功",
    "failed": "失败",
    "cleanup_error": "进程回收出错",
    "report_dropped": "丢弃上报",  # a worker report the service cannot use
}
_PROFILE_EVENTS = {
    "created": "新建配置",
    "updated": "修改配置",
    "deleted": "删除配置",
    "tested": "测试连接",  # the reason is the outcome
    "applied": "应用配置",
    "current_reset": "当前配置改回 Gemini",  # its file is gone, or it is no longer tested
}


class TaskQueue:
    """Task directories under ``<data_dir>/tasks`` plus the run queue.

    Construction touches no task; ``restore`` handles what an earlier
    service left, and ``start`` starts running tasks.
    """

    def __init__(
        self,
        data_dir: Path,
        env_file: Path,
        argon_env_file: Path,
        concurrency: int,
        hedging: bool = True,
        profiles_dir: Path = DEFAULT_PROFILES_DIR,
    ):
        self.tasks_dir = Path(data_dir) / "tasks"
        self.cache_dir = model_cache_dir(data_dir)
        self.env_file = Path(env_file)
        self.argon_env_file = Path(argon_env_file)
        self.concurrency = concurrency
        self.hedging = hedging  # False: each worker gets ``--no-hedging``
        self.profiles = ProfileStore(
            profiles_dir, data_dir, env_file, argon_env_file, concurrency, self._log_profile
        )
        # Waiting tasks: task ID -> (rank of its queue class, *sort key).
        self._waiting: dict[str, tuple] = {}
        # Tasks waiting for an automatic rerun: task ID -> (due time, submission time).
        self._due: dict[str, tuple[float, float]] = {}
        # Tasks whose processes could not be reaped: no new attempt starts
        # while there is one (spec section 2, "回收失败").
        self._cleanup_blocked: set[str] = set()
        # Tasks whose last reap failed, each with what to do once a retry
        # succeeds (or None); retried every REAP_RETRY_SECONDS.
        self._reap_retries: dict[str, Callable[[], None] | None] = {}
        # The one lock every state transition and the queue go through.
        self._cond = threading.Condition(threading.RLock())

    def restore(self) -> None:
        """Take over the tasks an earlier service left unfinished (spec sections 1 and 3).

        A task left running was interrupted: after its processes are reaped
        it is queued again, ahead of the others, unless this is its third
        interruption in a row, which fails it. A queued task keeps its queue
        class and sort key; a task waiting for a rerun keeps its due time.
        A task directory from before M3 keeps its final state; queued, it
        is queued as a first attempt; running, it fails.
        An error with one task fails that task alone, with a readable reason.

        The current model profile is checked first: one whose file is gone
        or that is no longer tested goes back to Gemini, with a log line.
        """
        self.profiles.current()
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        for status_file in sorted(self.tasks_dir.glob(f"*/{td.STATUS_FILE}")):
            task_id = status_file.parent.name
            try:
                self._restore_task(task_id)
            except Exception as e:  # noqa: BLE001 - one task's error must not stop the service
                self._fail_after_error(task_id, e, "翻译服务接管任务出错")

    def _restore_task(self, task_id: str) -> None:
        directory = self.tasks_dir / task_id
        status = td.read_status(directory)
        state = status["state"]
        if state in td.TERMINAL_STATES:
            if status.get("cleanup_error"):
                self._reap(task_id)  # a reap that failed before the restart: try again
            return
        if "attempt" not in status:  # written before M3: none of the new fields
            fields = {**_INITIAL_FIELDS, "queue_key": _submitted_at(directory)}
            if state == "running":
                # Its processes carry no marker: no reaping, no new attempt.
                self._decide(
                    task_id,
                    "failed",
                    {**fields, "state": "failed", "error": UPGRADE_ERROR, "failure_category": "retryable"},
                    reason=UPGRADE_ERROR,
                )
            elif state == "queued":
                with self._cond:
                    td.write_status(directory, **fields)
                    self._enqueue(task_id, FIRST_QUEUE_CLASS, fields["queue_key"])
            return
        # Left as it is until its processes are reaped (a failed reap is
        # retried). A cancel accepted before the restart is completed then,
        # whatever the state: no interruption, no rerun (``_reap``).
        self._reap(task_id, then=lambda: self._take_over(task_id))

    def _take_over(self, task_id: str) -> None:
        """Requeue, reschedule or fail a task an earlier service left, its processes reaped."""
        directory = self.tasks_dir / task_id
        status = td.read_status(directory)
        state = status["state"]
        if state == "queued":
            self._enqueue(task_id, status["queue_class"], status["queue_key"])
        elif state == "waiting_rerun":
            # Its saved due time stands; a rerun already queued is "queued".
            self._schedule_rerun(
                task_id, float(status["next_attempt_at"]), _submitted_at(directory)
            )
        elif state == "running":
            interruptions = status.get("consecutive_interruptions", 0) + 1
            if interruptions < MAX_INTERRUPTIONS:
                queue_key = _submitted_at(directory)
                self._decide(
                    task_id,
                    "interrupted",
                    {
                        "state": "queued",
                        "stage": td.QUEUED_STAGE,
                        "progress": 0,
                        "consecutive_interruptions": interruptions,
                        "queue_class": INTERRUPTED_QUEUE_CLASS,
                        "queue_key": queue_key,
                    },
                    outcome="interrupted",
                    reason=RESTART_ERROR,
                    then=(("requeued", None),),
                )
                self._enqueue(task_id, INTERRUPTED_QUEUE_CLASS, queue_key)
            else:
                self._decide(
                    task_id,
                    "interrupted",
                    {
                        "state": "failed",
                        "error": MANY_RESTARTS_ERROR,
                        "failure_category": "retryable",
                        "consecutive_interruptions": interruptions,
                    },
                    outcome="interrupted",
                    reason=RESTART_ERROR,
                    then=(("failed", MANY_RESTARTS_ERROR),),
                )

    def _enqueue(self, task_id: str, queue_class: str, queue_key: float | list) -> None:
        key = tuple(queue_key) if isinstance(queue_key, (list, tuple)) else (queue_key,)
        with self._cond:
            self._waiting[task_id] = (_QUEUE_RANK[queue_class], *key)
            self._cond.notify_all()

    def _schedule_rerun(self, task_id: str, due: float, submitted_at: float) -> None:
        with self._cond:
            self._due[task_id] = (due, submitted_at)
            self._cond.notify_all()

    def _queue_order(self) -> list[str]:
        """The waiting tasks in the order they will run."""
        return sorted(self._waiting, key=self._waiting.__getitem__)

    def _reap(self, task_id: str, then: Callable[[], None] | None = None) -> bool:
        """Reap the task's processes by marker (spec section 2); True once all have exited.

        On success the transition the task was waiting for is completed,
        under the same lock as the unblocking, so no other thread's failed
        reap of the task comes in between: an accepted cancel makes the
        task ``cancelled``; otherwise ``then()``, if given, runs.

        On failure the task keeps its state; ``cleanup_error`` and the log
        say why (logged again only when the reason changes), and ``then``
        is kept for the retry, which comes every REAP_RETRY_SECONDS
        (``_retry_reaps_forever``) and on a repeated cancel. Until every
        failed reap has succeeded, finished tasks' included, no new attempt
        starts.
        """
        try:
            processes.reap(task_id)
        except processes.ReapError as e:
            with self._cond:
                status = td.read_status(self.tasks_dir / task_id)
                reason = f"{e}；暂停调度新的尝试"
                if status.get("cleanup_error") != reason:
                    self._decide(task_id, "cleanup_error", {"cleanup_error": reason}, reason=reason)
                # A plain retry (then None, e.g. a repeated cancel) keeps
                # the transition an earlier failed reap was waiting for.
                if then is not None or task_id not in self._reap_retries:
                    self._reap_retries[task_id] = then
                self._cleanup_blocked.add(task_id)
            return False
        with self._cond:
            status = td.read_status(self.tasks_dir / task_id)
            if status.get("cleanup_error"):
                td.write_status(self.tasks_dir / task_id, cleanup_error=None)
            self._cleanup_blocked.discard(task_id)
            self._reap_retries.pop(task_id, None)
            try:
                if status.get("cancel_requested") and status["state"] not in td.TERMINAL_STATES:
                    self._finish_cancel(task_id)
                elif then is not None:
                    then()
            finally:
                self._cond.notify_all()  # scheduling may resume, even if then() failed
        return True

    def _retry_reaps_forever(self) -> None:
        """Every REAP_RETRY_SECONDS, reap again each task whose last reap failed."""
        while True:
            time.sleep(REAP_RETRY_SECONDS)
            with self._cond:
                failed = list(self._reap_retries.items())
            for task_id, then in failed:
                try:
                    self._reap(task_id, then=then)
                except Exception as e:  # noqa: BLE001 - one task's error must not stop the retries
                    self._fail_after_error(task_id, e)

    def start(self) -> None:
        threading.Thread(target=self._run_forever, name="task-runner", daemon=True).start()
        threading.Thread(target=self._rerun_forever, name="rerun-timer", daemon=True).start()
        threading.Thread(target=self._retry_reaps_forever, name="reap-retry", daemon=True).start()

    # -- automatic reruns ----------------------------------------------------

    def _rerun_forever(self) -> None:
        """Queue each waiting task when its rerun is due, by due time, then submission time."""
        while True:
            with self._cond:
                while True:
                    now = time.time()
                    order = sorted((due, submitted, t) for t, (due, submitted) in self._due.items())
                    if order and order[0][0] <= now:
                        break
                    self._cond.wait(order[0][0] - now if order else None)
                task_id = order[0][2]
                entry = self._due[task_id]
            # It stays among the due ones until it is queued, so the runner
            # does not start a first attempt meanwhile. Only this entry is
            # removed: once queued, the rerun's attempt may fail and schedule
            # the next rerun before this thread gets here.
            try:
                self._queue_rerun(task_id)
            except Exception as e:  # noqa: BLE001 - one task's error must not stop the reruns
                self._fail_after_error(task_id, e)
            finally:
                with self._cond:
                    if self._due.get(task_id) is entry:
                        del self._due[task_id]
                    self._cond.notify_all()

    def _queue_rerun(self, task_id: str) -> None:
        """The rerun is due: queue the task as a rerun and count it, in one transition."""
        directory = self.tasks_dir / task_id
        _test_pause(PAUSE_BEFORE_RERUN_QUEUE, task_id, td.read_status(directory)["attempt"])
        with self._cond:
            status = td.read_status(directory)
            # A cancel accepted while this thread waited (still reaping, or done) wins.
            if status["state"] != "waiting_rerun" or status.get("cancel_requested"):
                return
            queue_key = [status["next_attempt_at"], _submitted_at(directory)]
            self._decide(
                task_id,
                "rerun_started",
                {
                    "state": "queued",
                    "stage": td.QUEUED_STAGE,
                    "progress": 0,
                    "reruns_used": status["reruns_used"] + 1,
                    "next_attempt_at": None,
                    "queue_class": RERUN_QUEUE_CLASS,
                    "queue_key": queue_key,
                },
            )
            self._enqueue(task_id, RERUN_QUEUE_CLASS, queue_key)

    # -- the arbiter ---------------------------------------------------------

    def _decide(
        self,
        task_id: str,
        event: str,
        fields: dict,
        outcome: str | None = None,
        reason: str | None = None,
        then: tuple[tuple[str, str | None], ...] = (),
    ) -> dict:
        """Apply one state transition: save ``fields`` atomically and log ``event``.

        ``then`` gives further ``(event, reason)`` log lines of the same
        transition, e.g. the requeue after an interruption.

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
            for logged, why in ((event, reason), *then):
                self._log(task_id, logged, status.get("attempt", 0), why)
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
        records = path.parent / REQUESTS_FILE
        if records.exists() or "requests" not in report:
            # The request records file is the source of truth: it also holds
            # the sends that ended after the worker wrote its report, and
            # those of a worker that never wrote one.
            report["requests"] = load_records(records)
        if outcome in ("interrupted", "cancelled"):
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
        _print_log_line(line)

    def _log_profile(self, profile_id: str, event: str, name: str | None, reason: str | None) -> None:
        """One service log line about a model profile; never its key (``ProfileStore``)."""
        line = (
            f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}"
            f" profile={profile_id} event={_PROFILE_EVENTS[event]}"
            f" name={json.dumps(name, ensure_ascii=False)}"
        )
        if reason:
            line += f" reason={json.dumps(reason, ensure_ascii=False)}"
        _print_log_line(line)

    # -- HTTP side -----------------------------------------------------------

    def submit(self, pdf: bytes, filename: str, profile: str | None = None) -> str:
        """Queue a new task; it records the model profile ``profile``, or else
        the current one, for all its attempts. Raise ``ProfileError`` for a
        profile it cannot use (``ProfileStore.for_submission``), leaving no task."""
        profile_id, profile_name = self.profiles.for_submission(profile)
        task_id = uuid.uuid4().hex
        directory = self.tasks_dir / task_id
        directory.mkdir()
        (directory / td.SOURCE_PDF).write_bytes(pdf)
        submitted_at = time.time()
        (directory / "task.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "filename": filename,
                    "submitted_at": submitted_at,
                    "profile_id": profile_id,
                    "profile_name": profile_name,
                },
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
            self._enqueue(task_id, FIRST_QUEUE_CLASS, submitted_at)
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
            order = self._queue_order()
            position = order.index(task_id) + 1 if task_id in order else None
        state = status["state"]
        due = status.get("next_attempt_at")
        try:
            profile = _task_profile(directory)
        except (OSError, ValueError):
            profile = None
        return {
            "task_id": task_id,
            "state": state,
            "stage": status["stage"],
            "progress": status["progress"],
            "attempt": status.get("attempt", 0),
            "reruns_used": status.get("reruns_used", 0),
            "reruns_max": RERUNS_MAX,
            "next_attempt_in_seconds": (
                max(0, math.ceil(due - time.time()))
                if state == "waiting_rerun" and due is not None
                else None
            ),
            "queue_position": position if state == "queued" else None,
            # The reason of the last failure, while waiting for a rerun and once failed.
            "error": status.get("error") if state in ("waiting_rerun", "failed") else None,
            "cancel_requested": bool(status.get("cancel_requested", False)),
            "cleanup_error": status.get("cleanup_error"),
            "kept_source_paragraphs": status.get("kept_source_paragraphs", 0),
            "kept_source_algorithms": status.get("kept_source_algorithms", 0),
            "files": td.available_files(directory),
            "profile": profile,  # {"id", "name"} recorded at submission
        }

    def profile_users(self, profile_id: str) -> int:
        """How many unfinished tasks recorded the model profile ``profile_id``."""
        users = 0
        with self._cond:
            for status_file in self.tasks_dir.glob(f"*/{td.STATUS_FILE}"):
                try:
                    unfinished = td.read_status(status_file.parent)["state"] not in td.TERMINAL_STATES
                    if unfinished and _task_profile(status_file.parent)["id"] == profile_id:
                        users += 1
                except (OSError, ValueError, KeyError):
                    continue
        return users

    def cancel(self, task_id: str) -> tuple[int, dict] | None:
        """Cancel the task: ``(HTTP status, the task as status() gives it)``, or None if unknown.

        A task that has not finished (queued, running, waiting for a
        rerun): the accepted cancel is saved first, and from then on the
        task is neither started nor rerun, and its worker's reports are
        dropped; then every process with its marker is reaped, outside the
        lock, so queries are answered meanwhile; only when all have exited
        does the task become ``cancelled`` (200). When the reaping fails
        (within the reaping rule's 15 seconds), the task keeps its state,
        its ``cleanup_error`` says why, no new attempt starts, the service
        retries the reap every 30 seconds (``_reap``), and the answer is
        503; a repeated cancel tries again at once. A cancelled task
        answers 200 again, and when a reap of its processes failed after
        the cancel (``cleanup_error``), reaps again first; a succeeded or
        failed one 409, unchanged.
        """
        directory = self.task_dir(task_id)
        if directory is None:
            return None
        with self._cond:
            status = td.read_status(directory)
            state = status["state"]
            if state in td.TERMINAL_STATES and not (state == "cancelled" and status.get("cleanup_error")):
                return (200 if state == "cancelled" else 409), self.status(task_id)
            if state not in td.TERMINAL_STATES and not status.get("cancel_requested"):
                self._decide(task_id, "cancel_accepted", {"cancel_requested": True})
            self._waiting.pop(task_id, None)
            self._due.pop(task_id, None)
            self._cond.notify_all()
        if state == "cancelled":  # a reap that failed after the cancel: try again
            self._reap(task_id)
            return 200, self.status(task_id)
        _test_pause(PAUSE_AFTER_CANCEL_ACCEPTED, task_id, status.get("attempt", 0))
        self._reap(task_id)  # on success it completes the cancel
        # Another thread's reap (the runner's, a retry) may have completed it
        # even when this one failed: a cancelled task answers 200.
        answer = self.status(task_id)
        return (200 if answer["state"] == "cancelled" else 503), answer

    def _finish_cancel(self, task_id: str) -> None:
        """Complete an accepted cancel whose processes are reaped: ``cancelled``.

        ``cancel_requested`` is true only while the cancel is under way, so
        the cancelled state clears it. A running task's attempt ends as
        cancelled; its report is marked incomplete.
        """
        with self._cond:
            self._waiting.pop(task_id, None)
            self._due.pop(task_id, None)
            state = td.read_status(self.tasks_dir / task_id)["state"]
            if state in td.TERMINAL_STATES:
                return  # a concurrent cancel completed it
            ends_attempt = {"outcome": "cancelled", "reason": CANCEL_REASON} if state == "running" else {}
            self._decide(
                task_id,
                "cancelled",
                {"state": "cancelled", "next_attempt_at": None, "cancel_requested": False},
                **ends_attempt,
            )
            self._cond.notify_all()

    # -- the runner ----------------------------------------------------------

    def _next_task(self) -> str | None:
        """The waiting task to start next; None while there is none, scheduling
        is blocked, or a due rerun not yet queued would go ahead of it."""
        if not self._waiting or self._cleanup_blocked:
            return None
        task_id = self._queue_order()[0]
        if self._waiting[task_id][0] > _QUEUE_RANK[RERUN_QUEUE_CLASS]:
            now = time.time()
            if any(due <= now for due, _ in self._due.values()):
                return None
        return task_id

    def _run_forever(self) -> None:
        while True:
            with self._cond:
                while (task_id := self._next_task()) is None:
                    self._cond.wait()
            try:
                next_attempt = td.read_status(self.tasks_dir / task_id).get("attempt", 0) + 1
                _test_pause(PAUSE_BEFORE_REAP, task_id, next_attempt)
                with self._cond:
                    if task_id not in self._waiting:
                        continue  # cancelled meanwhile: the cancel reaped it
                # The processes of its earlier attempts go first. Meanwhile the
                # task stays queued, in its place, so a rerun that comes due or
                # a requeue can still go ahead of it.
                if not self._reap(task_id):
                    continue
                with self._cond:
                    if self._next_task() != task_id:
                        continue
                    # A cancel takes the task out of _waiting under this lock
                    # when it is accepted, so a task still here is not cancelled.
                    del self._waiting[task_id]
                    attempt = self._start_attempt(task_id)
                self._run_attempt(task_id, attempt)
            except Exception as e:  # noqa: BLE001 - one task's error must not stop the queue
                self._fail_after_error(task_id, e)

    def _fail_after_error(
        self,
        task_id: str,
        error: Exception,
        what: str = "翻译服务处理任务出错",
        category: str = "retryable",
        attempt_error: str | None = None,
        attempt_ended: bool = False,
    ) -> None:
        """End a task the service itself could not carry through; ``what`` begins the reason.

        An error while the service adopts an attempt's result
        (``attempt_ended``) ends that attempt as a failure of ``category`` (``retryable`` unless the attempt's own
        result was ``permanent``): a retryable one waits for an automatic
        rerun while reruns remain (spec section 1), else the task fails.
        When the attempt had failed on its own, ``attempt_error`` is its
        reason and stays the task's reason; the service's error is printed. An error at any
        other point (taking over at startup, queueing a due rerun, starting
        an attempt) fails the task.

        Best effort and never raises: the service goes on with the other
        tasks. The task's report.json is written if it can be; a status file
        that cannot be read is replaced by one that says the task failed.
        """
        service_error = f"{what}：{type(error).__name__}: {error}"
        print(f"task={task_id}: {service_error}", file=sys.stderr, flush=True)
        reason = attempt_error or service_error
        traceback.print_exception(error, file=sys.stderr)
        directory = self.tasks_dir / task_id
        with self._cond:
            self._waiting.pop(task_id, None)
            try:
                try:
                    status = td.read_status(directory)
                except (OSError, ValueError):
                    status = {"stage": td.QUEUED_STAGE, "progress": 0}
                    td.write_json(directory / td.STATUS_FILE, status)
                if status.get("state") in td.TERMINAL_STATES:
                    return
                reruns_used = status.get("reruns_used", 0)
                rerun = (
                    attempt_ended
                    and category == "retryable"
                    and status.get("state") == "running"
                    and reruns_used < RERUNS_MAX
                )
                changes = {
                    "state": "failed",
                    "error": reason,
                    "failure_category": category,
                    "consecutive_interruptions": 0,
                }
                if rerun:
                    due = time.time() + _rerun_waits()[reruns_used]
                    changes.update(state="waiting_rerun", next_attempt_at=due)
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
                self._log(task_id, changes["state"], status.get("attempt", 0), reason)
                if rerun:
                    self._schedule_rerun(task_id, changes["next_attempt_at"], _submitted_at(directory))
            except Exception:  # noqa: BLE001 - nothing more can be done for this task
                traceback.print_exc()

    def _start_attempt(self, task_id: str) -> int:
        """Save the task as running its next attempt; return the attempt number."""
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
                    "process_marker": processes.marker(task_id, attempt),
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
        return attempt

    def _run_attempt(self, task_id: str, attempt: int) -> None:
        """Run the started attempt and adopt its result."""
        try:
            result = self._run(task_id, attempt)
        except Exception as e:  # noqa: BLE001 - reported as this attempt's failure
            result = _failure(f"无法启动论文任务：{e}", "retryable")
        if result is None:
            return  # its end is decided elsewhere (_run)
        _test_pause(PAUSE_BEFORE_ADOPT, task_id, attempt)
        self._end_attempt(task_id, attempt, result)

    def _end_attempt(self, task_id: str, attempt: int, result: dict) -> None:
        """Adopt the attempt's result; an error of the service ends it as a failure."""
        try:
            self._adopt_result(task_id, attempt, result)
        except Exception as e:  # noqa: BLE001 - the attempt's own category stands
            failed = result["outcome"] == "failed"
            self._fail_after_error(
                task_id,
                e,
                category=result.get("category", "retryable") if failed else "retryable",
                attempt_error=result.get("error") if failed else None,
                attempt_ended=True,
            )

    def _adopt_result(self, task_id: str, attempt: int, result: dict) -> None:
        """End the attempt with its result: succeeded, waiting for a rerun, or failed."""
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
            directory = self.tasks_dir / task_id
            reruns_used = td.read_status(directory).get("reruns_used", 0)
            fields = {
                "error": result["error"],
                "failure_category": result["category"],
                "consecutive_interruptions": 0,
            }
            if result["category"] == "retryable" and reruns_used < RERUNS_MAX:
                due = time.time() + _rerun_waits()[reruns_used]
                # One lock for both, so a cancel never sees the task waiting
                # without its rerun scheduled (and leaves no stale entry).
                with self._cond:
                    if self._adopt(
                        task_id,
                        attempt,
                        "waiting_rerun",
                        {**fields, "state": "waiting_rerun", "next_attempt_at": due},
                        outcome="failed",
                        reason=result["error"],
                    ):
                        self._schedule_rerun(task_id, due, _submitted_at(directory))
            else:
                self._adopt(
                    task_id,
                    attempt,
                    "failed",
                    {**fields, "state": "failed"},
                    outcome="failed",
                    reason=result["error"],
                )

    def _run(self, task_id: str, attempt: int) -> dict | None:
        """Run one attempt's subprocess until it exits; return its result report.

        Return None when the attempt's end is not this thread's to adopt:
        a cancel accepted before the subprocess started (it never starts;
        the cancel ends the attempt), or a subprocess that exited without a
        result, whose failure is adopted only once its processes are reaped.
        """
        directory = self.tasks_dir / task_id
        marker = td.read_status(directory)["process_marker"]
        _test_pause(PAUSE_AFTER_MARKER, task_id, attempt)
        # Under the lock up to the start, so a cancel is accepted either
        # before (no start) or after (its reap finds the subprocess).
        with self._cond:
            status = td.read_status(directory)
            if status["state"] != "running" or status.get("cancel_requested"):
                return None
            process, read_end = self._start_worker(task_id, attempt, marker)
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
            # It may have left processes behind: the attempt fails only once
            # they are reaped; until then the task stays running (``_reap``).
            failure = _failure(f"论文任务进程异常退出（退出码 {exit_code}）", "retryable")
            _test_pause(PAUSE_BEFORE_ADOPT, task_id, attempt)
            self._reap(task_id, then=lambda: self._end_attempt(task_id, attempt, failure))
            return None
        return result

    def _start_worker(self, task_id: str, attempt: int, marker: str) -> tuple[subprocess.Popen, int]:
        """Start the attempt's subprocess; return it and the read end of its report pipe.

        The model settings are those of the profile the task recorded at
        submission: the start options for Gemini, else the profile's file,
        as it is now (an edit since, e.g. a new key, reaches the attempt).
        """
        directory = self.tasks_dir / task_id
        profile = _task_profile(directory)
        if profile["id"] == BUILTIN_ID:
            model_options = [
                "--env-file",
                str(self.env_file),
                "--argon-env-file",
                str(self.argon_env_file),
                "--concurrency",
                str(self.concurrency),
            ]
        else:
            model_options = ["--profile-file", str(self.profiles.path(profile["id"]))]
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
            *model_options,
            "--profile-id",
            profile["id"],
            "--profile-name",
            profile["name"],
            "--cache-dir",
            str(self.cache_dir),
        ]
        if not self.hedging:
            command.append("--no-hedging")
        try:
            with (directory / td.LOG_FILE).open("ab") as log:
                process = subprocess.Popen(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    pass_fds=(write_end,),
                    env={**os.environ, processes.MARK_ENV: marker},
                    # Its own process group: it ends the group when the
                    # service is gone (zotero_translate.worker).
                    start_new_session=True,
                )
        except BaseException:
            os.close(read_end)
            raise
        finally:
            os.close(write_end)
        return process, read_end

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


def _submitted_at(directory: Path) -> float:
    """When the task was submitted (its ``task.json``), the sort key within a queue class."""
    return float(json.loads((directory / "task.json").read_text("utf-8"))["submitted_at"])


def _task_profile(directory: Path) -> dict:
    """``{"id", "name"}`` of the model profile the task recorded; Gemini for a task from before M4."""
    task = json.loads((directory / "task.json").read_text("utf-8"))
    return {
        "id": task.get("profile_id") or BUILTIN_ID,
        "name": task.get("profile_name") or BUILTIN_NAME,
    }


def _print_log_line(line: str) -> None:
    try:
        print(line, flush=True)
    except (OSError, ValueError):  # no standard output: the transition stands
        pass


def _failure(error: str, category: str) -> dict:
    return {"outcome": "failed", "error": error, "category": category}


def _rerun_waits() -> tuple[float, float]:
    """Seconds to wait before the first and the second rerun.

    Test only: ``ZT_TEST_RERUN_WAITS_SECONDS`` replaces them.
    """
    setting = os.environ.get(RERUN_WAITS_SETTING)
    if not setting:
        return RERUN_WAITS_SECONDS
    first, second = (float(part) for part in setting.split(","))
    return first, second


def _download_delay(name: str) -> float:
    """Test only: seconds to wait before sending the file ``name`` (``ZT_TEST_DOWNLOAD_DELAY_SECONDS``)."""
    for part in filter(None, os.environ.get(DOWNLOAD_DELAY_SETTING, "").split(",")):
        file, _, seconds = part.partition("=")
        if file.strip() == name:
            return float(seconds)
    return 0.0


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
            if parts[0] == "profiles":
                return self._profiles("GET", parts[1:])
            if len(parts) == 2 and parts[0] == "tasks":
                status = queue.status(parts[1])
                if status is None:
                    return self._not_found()
                return self._json(200, status)
            if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "files":
                return self._send_file(parts[1], parts[3])
            return self._not_found()

        def do_PUT(self):
            self._profiles("PUT", self._profile_path())

        def do_DELETE(self):
            self._profiles("DELETE", self._profile_path())

        def do_OPTIONS(self):
            # No CORS preflight passes: a web page cannot send the profile
            # writes, which need a custom header and a JSON body.
            self._json(403, {"error": "翻译服务不接受跨源请求"})

        def _profile_path(self) -> list[str] | None:
            parts = self.path.split("?", 1)[0].strip("/").split("/")
            return parts[1:] if parts[0] == "profiles" else None

        def _profiles(self, method: str, rest: list[str] | None):
            """The model profile requests (see the module docstring)."""
            # The body is read first, whatever the answer: a socket closed
            # with unread data may reset the connection before the answer.
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if length else b""
            if rest is None:
                return self._not_found()
            if not self._local_host():
                return self._json(403, {"error": "请求的主机名不是本机翻译服务"})
            body = {}
            if method != "GET":
                body = self._write_body(data)
                if body is None:
                    return None
            store = queue.profiles
            try:
                if method == "GET" and rest == []:
                    return self._json(200, store.listing())
                if method == "POST" and rest == []:
                    return self._json(201, store.create(body))
                if len(rest) == 1 and method == "GET":
                    return self._json(200, store.get(rest[0]))
                if len(rest) == 1 and method == "PUT":
                    return self._json(200, store.update(rest[0], body))
                if len(rest) == 1 and method == "DELETE":
                    return self._json(200, store.delete(rest[0], queue.profile_users(rest[0])))
                if len(rest) == 2 and method == "POST" and rest[1] == "test":
                    # The request itself runs outside the store's lock.
                    settings, fingerprint = store.test_settings(rest[0])
                    check = check_connection(settings)
                    return self._json(200, store.record_test(rest[0], fingerprint, check))
                if len(rest) == 2 and method == "POST" and rest[1] == "apply":
                    return self._json(200, store.apply(rest[0]))
            except ProfileError as e:
                return self._json(e.status, {"error": str(e)})
            return self._json(404, {"error": "翻译服务没有这个接口"})

        def _local_host(self) -> bool:
            """Whether the Host header names this service (no page of another name resolved to it)."""
            port = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"{HOST}:{port}", f"localhost:{port}")

        def _write_body(self, data: bytes) -> dict | None:
            """The JSON object body of a profile write; None once a refusal is sent."""
            if self.headers.get(WRITE_HEADER) != "1":
                self._json(403, {"error": f"缺少请求头 {WRITE_HEADER}: 1"})
                return None
            media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                self._json(415, {"error": "请求正文要是 JSON（Content-Type: application/json）"})
                return None
            try:
                body = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                body = None
            if not isinstance(body, dict):
                self._json(400, {"error": "请求正文不是 JSON 对象"})
                return None
            return body

        def do_POST(self):
            parts = self.path.split("?", 1)[0].strip("/").split("/")
            if parts[0] == "profiles":
                return self._profiles("POST", parts[1:])
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
                answer = queue.cancel(parts[1])
                if answer is None:
                    return self._not_found()
                return self._json(*answer)
            if parts != ["tasks"]:
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
            profile = (fields.get("profile") or (b"", None))[0].decode("utf-8", errors="replace")
            try:
                task_id = queue.submit(pdf, filename, profile or None)  # empty: the current one
            except ProfileError as e:
                return self._json(e.status, {"error": str(e)})
            return self._json(201, {"task_id": task_id})

        def _send_file(self, task_id: str, name: str):
            directory = queue.task_dir(task_id)
            file = td.DOWNLOAD_FILES.get(name)
            if directory is None or file is None or not (directory / file).is_file():
                return self._not_found()
            time.sleep(_download_delay(name))
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
    data_dir: Path,
    env_file: Path,
    argon_env_file: Path,
    port: int,
    concurrency: int,
    hedging: bool = True,
    profiles_dir: Path = DEFAULT_PROFILES_DIR,
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
    queue = TaskQueue(data_dir, env_file, argon_env_file, concurrency, hedging, profiles_dir)
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
