"""Fake pipeline model: a local OpenAI-compatible chat completions server.

Test fixture only. It can:

- record every request (model, parameters, messages) for the test to read;
- answer by request kind with pre-written responders;
- inject errors (500, 429, timeout, slow, empty content, invalid JSON) on
  the N-th request of a kind, or on the first request of a kind and then on
  each resend of that same request (``fail_same_request``). "timeout" sends
  nothing for an hour; "slow" sends the response headers at once and then the
  normal answer a few bytes at a time over ``slow_body_seconds``;
- inject a delay and count the maximum number of requests in flight.

Request kinds are recognised from the message content:

- ``dossier``: the paper dossier request, recognised by ``DOSSIER_MARKER``
  in its instructions. The default answer is ``FAKE_DOSSIER`` as JSON.
- ``term_list``: the term list request, recognised by ``TERM_LIST_MARKER``
  in its instructions. The default answer is ``FAKE_TERM_LIST`` as JSON;
  every spelling in it appears on page 1 of AutoDesign.
- ``translate_batch``: an engine paragraph batch, i.e. the prompt ends with a
  JSON array of ``{"id", "input", ...}`` objects. The default answer is a
  deterministic pseudo-translation: ``PSEUDO_PREFIX`` + the input, so
  placeholders, numbers and key concepts are kept.
- ``review``: the review of a batch's drafts, recognised by ``REVIEW_MARKER``;
  it ends with a JSON array of ``{"id", "source", "draft", ...}`` objects.
  The default answer returns every draft unchanged.
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

# A phrase of the paper dossier request's instructions.
DOSSIER_MARKER = "Return the paper dossier"

FAKE_DOSSIER = {
    "paper_title": "AutoDesign: Meta-Harness Optimization for Long-Horizon Agentic Design",
    "discipline": "假学科：计算机视觉",
    "paper_summary_zh": "假概要：本文提出 AutoDesign 框架。",
    "section_summaries_zh": [{"section": "8 Conclusion", "summary_zh": "假章节摘要：结论。"}],
    "translation_pitfalls_zh": ["假易错点：harness 不要译成线束。"],
}

# A phrase of the term list request's instructions.
TERM_LIST_MARKER = "Return the term list"

FAKE_TERM_LIST = {
    "terms": [
        {
            "english": ["meta-harness"],
            "category": "key_concept",
            "chinese": None,
            "gloss_zh": "假释义：优化框架本身的外层框架",
            "reason": "假依据：本文的核心方法。",
        },
        {
            "english": ["code agent"],
            "category": "basic_term",
            "chinese": "代码智能体",
            "gloss_zh": None,
            "reason": "假依据：已有通行译法。",
        },
    ]
}

# A phrase of the review request's instructions.
REVIEW_MARKER = "Review the draft translation"

_ERRORS = {"500", "429", "timeout", "slow", "empty", "invalid_json"}

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
    fault: str | None = None  # the injected error; None if answered normally
    received_at: float = 0.0  # time.monotonic() when the request arrived

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
class _SameRequestFault:
    kind: str | None
    errors: list[str]
    body: dict | None = None  # the first matching request; resends must equal it


@dataclass
class FakeModel:
    """Start with ``with FakeModel() as model:``; ``model.base_url`` is the endpoint."""

    delay: float = 0.0
    slow_body_seconds: float = 30.0  # how long a "slow" fault takes to send its body
    requests: list[RecordedRequest] = field(default_factory=list)
    max_in_flight: int = 0

    def __post_init__(self):
        self._lock = threading.Lock()
        self._in_flight = 0
        self._faults: list[_Fault] = []
        self._same_request_faults: list[_SameRequestFault] = []
        self._responders: dict[str, Responder] = {
            "dossier": lambda body: json.dumps(FAKE_DOSSIER, ensure_ascii=False),
            "term_list": lambda body: json.dumps(FAKE_TERM_LIST, ensure_ascii=False),
            "translate_batch": pseudo_translate_batch,
            "review": keep_drafts,
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

        ``error`` is one of: "500", "429", "timeout", "slow", "empty", "invalid_json".
        """
        assert error in _ERRORS, error
        self._faults.append(_Fault(kind=kind, nth=nth, error=error))

    def fail_same_request(self, errors: list[str], *, kind: str | None = None) -> None:
        """Fail the first request of ``kind``, then each identical resend, with ``errors`` in turn.

        After the errors are used up, that request is answered normally.
        """
        assert errors and all(e in _ERRORS for e in errors), errors
        self._same_request_faults.append(_SameRequestFault(kind=kind, errors=list(errors)))

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
            fault = self._take_same_request_fault(kind, body) or self._take_fault(kind)
            self.requests.append(
                RecordedRequest(
                    kind=kind,
                    path=handler.path,
                    headers=dict(handler.headers.items()),
                    body=body,
                    fault=fault,
                    received_at=time.monotonic(),
                )
            )
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
            payload = _completion(body.get("model", ""), content)
            if fault == "slow":
                return _send_json_slowly(handler, payload, self.slow_body_seconds)
            _send_json(handler, 200, payload)
        finally:
            with self._lock:
                self._in_flight -= 1

    def _take_same_request_fault(self, kind: str, body: dict) -> str | None:
        for fault in self._same_request_faults:
            if not fault.errors:
                continue
            if fault.body is None and fault.kind in (None, kind):
                fault.body = body
            if fault.body == body:
                return fault.errors.pop(0)
        return None

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
    if REVIEW_MARKER in content:
        return "review"
    if batch_items(content) is not None:
        return "translate_batch"
    instructions = [m.get("content") or "" for m in body.get("messages", [])]
    if any(TERM_LIST_MARKER in text for text in instructions):
        return "term_list"
    if any(DOSSIER_MARKER in text for text in instructions):
        return "dossier"
    return "other"


def batch_items(content: str) -> list[dict] | None:
    """Return the trailing JSON array of ``{"id", "input"}`` items, if any."""
    return _trailing_items(content, "input")


def review_items(content: str) -> list[dict] | None:
    """Return the trailing JSON array of ``{"id", "source", "draft"}`` items, if any."""
    return _trailing_items(content, "draft")


def _trailing_items(content: str, key: str) -> list[dict] | None:
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
            and all(isinstance(i, dict) and "id" in i and key in i for i in value)
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


def keep_drafts(body: dict) -> str:
    """A review answer that returns every draft unchanged."""
    items = review_items(_last_user_content(body)) or []
    return json.dumps(
        [{"id": i["id"], "output": i["draft"]} for i in items], ensure_ascii=False
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


def _send_json_slowly(handler: BaseHTTPRequestHandler, payload: dict, seconds: float) -> None:
    """Send a 200 header at once, then the body in small pieces spread over ``seconds``."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    size = -(-len(data) // max(1, int(seconds / 0.1)))  # about one piece per 0.1 s
    pieces = [data[i : i + size] for i in range(0, len(data), size)]
    try:
        for piece in pieces:
            time.sleep(seconds / len(pieces))
            handler.wfile.write(piece)
    except (BrokenPipeError, ConnectionResetError):
        pass  # the client gave up on the response
