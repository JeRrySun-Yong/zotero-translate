"""Request latency records: one line per model request send of an attempt (spec section 7).

The pipeline model client appends a record for every send, and one for every
answer taken from the cache, to ``requests.jsonl`` next to the attempt's task
report, as soon as the send ends: one JSON object per line, written at once,
so that a killed process leaves every finished record behind.

``requests.jsonl`` is the source of truth. The task report
(``zotero_translate.task_report``) carries the records as ``requests`` as far
as they existed when it was written; sends can still end after that (a
failed paper task does not wait for its algorithm requests). When an attempt
ends (after the worker process has exited), the translation service
replaces the report's ``requests`` with the file's (``load_records``).
``run`` does the same with ``update_report`` as its process exits, after
Python has joined the threads still sending.

A record has these fields:

- ``attempt``: the attempt number (None for ``run``);
- ``request``: the logical request, numbered from 1 within the attempt; the
  resends of one request share it;
- ``kind``: the request kind, one of ``KINDS``;
- ``stage``: the stage the request belongs to: for the engine's requests
  the translation stage (``KIND_STAGES``), for the others the paper task's
  stage when the request started (its resends keep it);
- ``page``: the page (1-based) the request comes from, or None;
- ``send``: which send of the request this is, from 1; None for a cache hit;
- ``endpoint`` and ``model``: the endpoint role (``flash`` or ``argon``) and
  model name the send went to; for a cache hit, those that gave the cached
  answer (the client's own for an entry cached before these records);
- ``started_at``: the wall-clock time (Unix seconds) the send went out;
- ``slot_wait_seconds``: the time spent waiting for a concurrency slot;
- ``seconds``: from sending to the end (last byte read, timeout, error);
  0 for a cache hit;
- ``result``: one of ``RESULTS``; with ``http_error``, ``http_status`` is the
  status code (it is set whenever a response arrived).

No record holds a message, an answer, a header or an API key.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from zotero_translate.paper_task import TRANSLATE_STAGE

REQUESTS_FILE = "requests.jsonl"

# Request kinds: the paper dossier, the paper's term list, the promoted
# glosses request (释义补写), the gloss rewrite (释义重写), a translation
# batch, the review of a batch, the translation of an algorithm and its
# review, and the engine's one-paragraph translation after a failed batch
# (逐段重译).
KINDS = (
    "dossier",
    "term_list",
    "promoted_glosses",
    "gloss_rewrite",
    "translation_batch",
    "review",
    "algorithm_translation",
    "algorithm_review",
    "single_paragraph",
)

# The stage of the requests the engine sends: it sends them all while it
# translates. The paper task's progress reports lag behind the engine (a
# batch can go out while they still say layout parsing), so these kinds take
# their stage from here; the others, sent while the paper task itself is in
# their stage, take it from the progress reports (``RequestLog.stage``).
KIND_STAGES = {
    kind: TRANSLATE_STAGE
    for kind in (
        "translation_batch",
        "review",
        "algorithm_translation",
        "algorithm_review",
        "single_paragraph",
    )
}

# Send results: an answer the caller accepted; a send cut off at the request
# timeout; an HTTP error status; a network error before an answer; an answer
# that was empty, did not parse or that the caller's check rejected; any other
# error that ended the send (e.g. a closed client), which the caller then
# sees unchanged; an answer from the cache (no send).
RESULTS = (
    "succeeded",
    "timeout",
    "http_error",
    "network_error",
    "invalid_answer",
    "error",
    "cache_hit",
)


class RequestLog:
    """The request records of one attempt, appended to ``path`` as they happen."""

    def __init__(self, path: Path, attempt: int | None = None):
        self.path = Path(path)
        self.attempt = attempt
        self.stage: str | None = None  # set by the task report as the paper task moves on
        self.records: list[dict] = []
        self._lock = threading.Lock()
        self._next_request = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")  # a new attempt starts empty

    def new_request(self) -> int:
        """The number of a new logical request."""
        with self._lock:
            self._next_request += 1
            return self._next_request

    def append(self, **fields) -> None:
        """Add a record to the file at once.

        There is no closing: a send that ends after the task report was
        written (e.g. an algorithm request still in flight when the paper
        task failed) is still appended, as long as the process lives.
        """
        record = {"attempt": self.attempt, **fields}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self.records.append(record)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")


def update_report(report_path: Path) -> None:
    """Set the ``requests`` of the task report at ``report_path`` to its ``requests.jsonl``.

    Nothing happens when the report does not exist or does not parse.
    """
    report_path = Path(report_path)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    records = load_records(report_path.parent / REQUESTS_FILE)
    if not isinstance(report, dict) or report.get("requests") == records:
        return
    report["requests"] = records
    tmp = report_path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, report_path)


def load_records(path: Path) -> list[dict]:
    """The records in a request log file; [] if it is missing.

    Each line is decoded on its own, so a line cut short by a killed process,
    even in the middle of a UTF-8 sequence, loses only itself.
    """
    try:
        lines = Path(path).read_bytes().split(b"\n")
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line.decode("utf-8"))
        except ValueError:  # UnicodeDecodeError included
            continue
        if isinstance(record, dict):
            records.append(record)
    return records
