"""Fake pipeline model: a local OpenAI-compatible chat completions server.

Test fixture only. It can:

- record every request (model, parameters, messages) for the test to read;
- answer by request kind with pre-written responders;
- inject errors (500, 429, timeout, empty content, invalid JSON) on the
  N-th request of a kind;
- inject a delay and count the maximum number of requests in flight.

Request kinds are recognised from the message content:

- ``translate_batch``: an engine paragraph batch, i.e. the prompt ends with a
  JSON array of ``{"id", "input", ...}`` objects. The default answer is a
  deterministic pseudo-translation: ``PSEUDO_PREFIX`` + the input, so
  placeholders, numbers and key concepts are kept.
- ``single_paragraph``: the engine's one-paragraph prompts, which it uses to
  redo a paragraph after a failed or rejected batch.
- ``other``: anything else.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Callable

PSEUDO_PREFIX = "伪译文"

# Markers of the engine's one-paragraph prompts, each followed by the text.
SINGLE_PARAGRAPH_MARKERS = (
    "Now translate the following text:\n\n",
    ";; Treat next line as plain text input",
)

Responder = Callable[[dict], str]


@dataclass
class RecordedRequest:
    kind: str
    path: str
    headers: dict[str, str]
    body: dict

    @property
    def prompt(self) -> str:
        return "\n".join(m.get("content", "") for m in self.body.get("messages", []))


@dataclass
class _Fault:
    kind: str | None
    nth: int
    error: str
    seen: int = 0
    fired: bool = False


@dataclass
class FakeModel:
    """Start with ``with FakeModel() as model:``; ``model.base_url`` is the endpoint."""

    delay: float = 0.0
    requests: list[RecordedRequest] = field(default_factory=list)
    max_in_flight: int = 0

    def __post_init__(self):
        self._lock = threading.Lock()
        self._in_flight = 0
        self._faults: list[_Fault] = []
        self._responders: dict[str, Responder] = {
            "translate_batch": pseudo_translate_batch,
            "single_paragraph": pseudo_translate_single,
            "other": lambda body: "ok",
        }
        self._server: ThreadingHTTPServer | None = None

    # -- configuration -------------------------------------------------

    def respond(self, kind: str, responder: Responder) -> None:
        """Answer requests of ``kind`` with ``responder(body) -> content``."""
        self._responders[kind] = responder

    def inject_error(self, error: str, *, kind: str | None = None, nth: int = 1) -> None:
        """Fail the ``nth`` request of ``kind`` (any kind if None) once.

        ``error`` is one of: "500", "429", "timeout", "empty", "invalid_json".
        """
        assert error in {"500", "429", "timeout", "empty", "invalid_json"}, error
        self._faults.append(_Fault(kind=kind, nth=nth, error=error))

    # -- inspection ----------------------------------------------------

    def requests_of(self, kind: str) -> list[RecordedRequest]:
        with self._lock:
            return [r for r in self.requests if r.kind == kind]

    # -- lifecycle -----------------------------------------------------

    @property
    def base_url(self) -> str:
        assert self._server is not None, "fake model not started"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> FakeModel:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                fake._handle(self, body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # -- request handling ----------------------------------------------

    def _handle(self, handler: BaseHTTPRequestHandler, body: dict) -> None:
        kind = classify(body)
        with self._lock:
            self.requests.append(
                RecordedRequest(
                    kind=kind,
                    path=handler.path,
                    headers=dict(handler.headers.items()),
                    body=body,
                )
            )
            fault = self._take_fault(kind)
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            if fault == "500":
                return _send_json(handler, 500, {"error": {"message": "injected 500"}})
            if fault == "429":
                return _send_json(handler, 429, {"error": {"message": "injected 429"}})
            if fault == "timeout":
                time.sleep(3600)
                return None
            if fault == "empty":
                content = ""
            elif fault == "invalid_json":
                content = "this is not json {"
            else:
                content = self._responders[kind](body)
            _send_json(handler, 200, _completion(body.get("model", ""), content))
        finally:
            with self._lock:
                self._in_flight -= 1

    def _take_fault(self, kind: str) -> str | None:
        for fault in self._faults:
            if fault.fired or (fault.kind is not None and fault.kind != kind):
                continue
            fault.seen += 1
            if fault.seen == fault.nth:
                fault.fired = True
                return fault.error
        return None


# -- request kinds and default responders --------------------------------


def classify(body: dict) -> str:
    content = _last_user_content(body)
    if any(marker in content for marker in SINGLE_PARAGRAPH_MARKERS):
        return "single_paragraph"
    if batch_items(content) is not None:
        return "translate_batch"
    return "other"


def batch_items(content: str) -> list[dict] | None:
    """Return the trailing JSON array of ``{"id", "input"}`` items, if any."""
    decoder = json.JSONDecoder()
    start = content.rfind("\n[")
    while start != -1:
        try:
            value, end = decoder.raw_decode(content, start + 1)
        except json.JSONDecodeError:
            value, end = None, -1
        if (
            end == len(content.rstrip())
            and isinstance(value, list)
            and value
            and all(isinstance(i, dict) and "id" in i and "input" in i for i in value)
        ):
            return value
        start = content.rfind("\n[", 0, start)
    return None


def pseudo_translate_batch(body: dict) -> str:
    items = batch_items(_last_user_content(body)) or []
    return json.dumps(
        [{"id": i["id"], "output": PSEUDO_PREFIX + i["input"]} for i in items],
        ensure_ascii=False,
    )


def echo_batch(body: dict) -> str:
    """Return every batch input unchanged (an "untranslated" answer)."""
    items = batch_items(_last_user_content(body)) or []
    return json.dumps(
        [{"id": i["id"], "output": i["input"]} for i in items], ensure_ascii=False
    )


def pseudo_translate_single(body: dict) -> str:
    content = _last_user_content(body)
    for marker in ("Now translate the following text:\n\n", "Input:\n\n"):
        if marker in content:
            content = content.split(marker, 1)[1]
            break
    return PSEUDO_PREFIX + content


def _last_user_content(body: dict) -> str:
    for message in reversed(body.get("messages", [])):
        if message.get("role") == "user":
            return message.get("content") or ""
    return ""


def _completion(model: str, content: str) -> dict:
    return {
        "id": "fake-completion",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)
