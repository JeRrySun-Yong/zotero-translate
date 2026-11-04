"""Pipeline model client: every model request of a paper task goes through here.

It calls an OpenAI-compatible chat completions endpoint with the configured
model, reasoning effort, temperature and timeout, and caps the number of
requests in flight with the endpoint's budget, shared by the whole paper
task (``zotero_translate.endpoint``).

Reliability (ADR 0002):

- The request timeout bounds the whole request, from sending it to reading
  the last byte of the response (see ``_post``). A send that runs past it
  is cancelled; its slot comes back once its connection has closed.
- Network errors, timeouts, HTTP 400, 429 and 5xx, empty content and
  answers the caller's ``validate`` rejects are retried, at most
  ``max_retries`` times. Other HTTP errors (e.g. 401, 403) fail at once.
  When the attempts are used up, ``complete`` raises ``ModelCallError`` with
  a readable Chinese reason and a failure category: ``permanent`` for HTTP
  401 and 403 (the key), ``retryable`` for everything else.
- Before the n-th resend, a network error (the endpoint cannot be reached,
  or the connection breaks before an answer; not a timeout, not an HTTP
  status) waits ``connection_retry_seconds[n - 1]``, by default 5, 15 and 45
  seconds; every other retried failure waits ``retry_backoff_seconds`` and
  then twice as long each time, by default 1, 2 and 4 seconds. The network
  waits together outlast a network outage of about a minute, which failed
  two papers of the closure-02 run (2026-10-28 18:22) under the 1, 2 and 4
  seconds that suit an overloaded endpoint. The number of attempts is the
  same for every kind of failure.
- HTTP 400 is retried because the relay endpoint answered a valid request
  with 400 once and the same request succeeded when the paper was submitted
  again (closure-01 run2, MaLiang page 7).
- The reason of an HTTP error, in the retry warning and in the final
  ``ModelCallError``, carries the first ``ERROR_BODY_CHARACTERS``
  characters of the response body on one line, so that the task log shows
  why the endpoint refused. A body can echo the request, so before it is
  cut, the API key of this client and of its fallback is replaced with
  ``[redacted]``; HTTP 401 and 403, which are about the key, carry no body
  at all. The request headers are never logged.
- Successful answers (those ``validate`` accepts) are cached on disk, keyed by
  a hash of the whole request body, which includes the model name. The cache
  directory is shared by all paper tasks, so a resubmitted paper reuses every
  answer that succeeded before.

Two endpoints (ADR 0004): ``PipelineModels`` holds a client for flash and
one for argon. A flash request whose attempts time out twice in a row sends
its remaining attempts to argon (its ``fallback``):

- Only timeouts count; any other failed attempt (HTTP 400, 429 or 5xx, a
  network error, empty content, a rejected answer) sets the count back to 0.
- A forwarded request stays on argon, and its attempts stay ``1 +
  max_retries`` in total. Argon's attempts take a slot of argon's
  endpoint budget, which all argon requests share.
- A forwarded answer is cached under the flash request, so a resubmitted
  paper finds it without sending anything. A cache entry names the endpoint
  role and model that gave the answer.

One budget per endpoint (spec section 7): when flash and argon have the
same address and API key, they are one endpoint and share one budget, of
flash's concurrency setting; argon's own setting is ignored, and the log
says so. The engine's own concurrency still comes from flash's setting.

Closing a client cancels its sends in flight (a failed paper task does not
wait for its algorithm requests to time out); they raise ``SendCancelled``.

Request latency records (spec section 7, ``zotero_translate.request_log``):
with a ``request_log``, every send gets a record when it ends, and so does
every answer from the cache. A send's time is counted from the moment it
goes out, after its concurrency slot is taken, to its end; the wait for the
slot is recorded on its own.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from zotero_translate.endpoint import Endpoint
from zotero_translate.request_log import KIND_STAGES
from zotero_translate.request_log import RequestLog
from zotero_translate.settings import ModelSettings
from zotero_translate.settings import PipelineSettings

logger = logging.getLogger(__name__)

Validator = Callable[[str], object]


class ModelCallError(Exception):
    """A model request failed for good; the message is a readable Chinese reason.

    ``answered`` is True when at least one attempt got an answer that the
    caller's ``validate`` rejected, i.e. the endpoint did answer.

    ``category`` is the failure category (spec section 1): ``permanent`` for
    a credential error (HTTP 401, 403), which a rerun of the paper task
    cannot fix; ``retryable`` for every other failure.
    """

    def __init__(self, message: str, answered: bool = False, category: str = "retryable"):
        super().__init__(message)
        self.answered = answered
        self.category = category


# Consecutive timeouts after which a flash request goes to argon (ADR 0004).
FORWARD_AFTER_TIMEOUTS = 2
ERROR_BODY_CHARACTERS = 200  # of an HTTP error's response body, in its reason
REDACTED = "[redacted]"  # replaces an API key echoed in an error response


class _RetryableError(Exception):
    """One attempt failed in a way that a resend may fix."""


class _Timeout(_RetryableError):
    """One attempt ran past the request timeout."""


class _NetworkError(_RetryableError):
    """One attempt could not reach the endpoint, or lost the connection before an answer."""


class _HttpError(_RetryableError):
    """One attempt got an HTTP error status that a resend may fix (400, 429, 5xx)."""


@dataclass
class _SendTiming:
    """One send's timing for its record (``zotero_translate.request_log``)."""

    started_at: float | None = None  # wall clock, as the send goes out after its slot
    slot_wait_seconds: float = 0.0  # waiting for a concurrency slot
    seconds: float = 0.0  # from going out to the end
    http_status: int | None = None  # once a response arrived
    sent: float = 0.0  # monotonic clock as the send goes out; not recorded


class ModelClient:
    """One endpoint's client: its model, its concurrency limit, the cache and the retries.

    ``fallback`` is the client that takes over a request after
    ``FORWARD_AFTER_TIMEOUTS`` timeouts in a row; None never forwards.
    ``role`` (``flash`` or ``argon``) names the endpoint in the request
    records, which go to ``request_log``; None records nothing.
    ``endpoint`` holds the HTTP client and the concurrency budget; by default
    the client has one of its own, with ``settings.concurrency`` slots.
    """

    def __init__(
        self,
        settings: ModelSettings,
        fallback: ModelClient | None = None,
        role: str | None = None,
        request_log: RequestLog | None = None,
        endpoint: Endpoint | None = None,
    ):
        self.settings = settings
        self.fallback = fallback
        self.role = role
        self.request_log = request_log
        self.endpoint = endpoint if endpoint is not None else Endpoint(settings)
        self._cache = ResponseCache(settings.cache_dir) if settings.cache_dir else None
        self._lock = threading.Lock()
        self._in_flight = 0  # this client's sends whose connection has not ended
        self.request_count = 0  # HTTP requests sent to this endpoint, resends included
        self.timeout_count = 0  # of those, the ones that ran past the request timeout
        self.cache_hits = 0
        self.retry_count = 0
        self.max_in_flight = 0
        self.forwarded_pages: list[int | None] = []  # one per request sent on to ``fallback``

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def concurrency(self) -> int:
        """The size of the endpoint's budget, which another client may share."""
        return self.endpoint.concurrency

    def complete(
        self,
        messages: list[dict],
        validate: Validator | None = None,
        page: int | None = None,
        kind: str | None = None,
    ) -> str:
        """Return the stripped message content of one chat completion.

        ``validate(content)`` raises if the content is unusable (e.g. it does
        not parse); such an answer is retried like a failed request and is
        never cached. ``page`` (1-based) is where the request comes from; the
        task report lists it when the request is forwarded. ``kind`` is the
        request kind of its records (``request_log.KINDS``).
        """
        body = self._body(messages)
        key = ResponseCache.key(body)
        log = self.request_log
        request = log.new_request() if log is not None else None
        # The stage the request belongs to: by its kind for the engine's
        # requests, else the paper task's stage as the request starts.
        stage = KIND_STAGES.get(kind, log.stage) if log is not None else None

        def record(
            endpoint: str | None, model: str, send: int | None, timing: _SendTiming, result: str
        ) -> None:
            if log is not None:
                log.append(
                    request=request,
                    kind=kind,
                    stage=stage,
                    page=page,
                    send=send,
                    endpoint=endpoint,
                    model=model,
                    started_at=timing.started_at,
                    slot_wait_seconds=round(timing.slot_wait_seconds, 3),
                    seconds=round(timing.seconds, 3),
                    result=result,
                    http_status=timing.http_status,
                )

        if self._cache is not None:
            cached = self._cache.entry(key)
            if cached is not None:
                with self._lock:
                    self.cache_hits += 1
                # The endpoint that answered (a forwarded answer is argon's);
                # an entry from before the records says nothing: this client.
                record(
                    cached.get("endpoint", self.role),
                    cached.get("model", self.model),
                    None,
                    _SendTiming(started_at=time.time()),
                    "cache_hit",
                )
                return cached["content"]
        attempts = 1 + self.settings.max_retries
        answered = False
        target, target_body = self, body
        timeouts_in_a_row = 0
        forwarded_from = None  # the attempt number of the first attempt on the fallback
        for attempt in range(1, attempts + 1):
            timing = _SendTiming()
            try:
                try:
                    content = target._send(target_body, timing)
                except ModelCallError:  # an HTTP error no resend fixes
                    record(target.role, target.model, attempt, timing, "http_error")
                    raise
                except _RetryableError:  # recorded below
                    raise
                except BaseException:
                    # Anything else (e.g. a closed client) still ends a send
                    # that went out; the error propagates unchanged.
                    if timing.started_at is not None:
                        record(target.role, target.model, attempt, timing, "error")
                    raise
                if validate is not None:
                    try:
                        validate(content)
                    except Exception as e:  # noqa: BLE001 - any rejection means "unparseable"
                        answered = True
                        raise _RetryableError(f"无法解析管线模型的响应：{e}") from e
            except _RetryableError as e:
                record(target.role, target.model, attempt, timing, _result_of(e))
                timeouts_in_a_row = timeouts_in_a_row + 1 if isinstance(e, _Timeout) else 0
                if attempt == attempts:
                    where = (
                        f"，第 {forwarded_from} 次起发给 {target.model}" if forwarded_from else ""
                    )
                    raise ModelCallError(f"{e}（共尝试 {attempts} 次{where}）", answered) from e
                if (
                    target is self
                    and self.fallback is not None
                    and timeouts_in_a_row >= FORWARD_AFTER_TIMEOUTS
                ):
                    target, target_body = self.fallback, self.fallback._body(messages)
                    forwarded_from = attempt + 1
                    with self._lock:
                        self.forwarded_pages.append(page)
                    logger.warning(
                        "model request timed out %d times in a row (page %s); "
                        "sending the remaining attempts to %s",
                        timeouts_in_a_row,
                        page,
                        target.model,
                    )
                wait = self._wait_before_resend(attempt, e)
                logger.warning(
                    "model request failed (attempt %d/%d): %s; retrying in %.1fs",
                    attempt,
                    attempts,
                    e,
                    wait,
                )
                with self._lock:
                    self.retry_count += 1
                time.sleep(wait)
                continue
            record(target.role, target.model, attempt, timing, "succeeded")
            if self._cache is not None:
                self._cache.put(key, content, endpoint=target.role, model=target.model)
            return content
        raise AssertionError("unreachable")

    def _wait_before_resend(self, attempt: int, error: _RetryableError) -> float:
        """Seconds to wait after the failed ``attempt`` (1-based) before the next one."""
        if isinstance(error, _NetworkError):
            waits = self.settings.connection_retry_seconds
            return waits[min(attempt, len(waits)) - 1]
        return self.settings.retry_backoff_seconds * 2 ** (attempt - 1)

    def _body(self, messages: list[dict]) -> dict:
        return {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "reasoning_effort": self.settings.reasoning_effort,
        }

    def _send(self, body: dict, timing: _SendTiming) -> str:
        """Send ``body`` once; fill ``timing`` as it goes, also when this raises."""
        try:
            status_code, data = self._post(body, timing)
        except _Timeout:
            self._count_timeout()
            raise
        except httpx.ConnectError as e:
            raise _NetworkError("无法连接管线模型") from e
        except httpx.TimeoutException as e:
            self._count_timeout()
            raise _Timeout(self._timeout_reason()) from e
        except httpx.TransportError as e:
            raise _NetworkError(f"与管线模型的网络连接出错：{type(e).__name__}") from e
        finally:
            if timing.started_at is not None:
                timing.seconds = time.monotonic() - timing.sent
        timing.http_status = status_code
        if status_code == 429:
            raise _HttpError("管线模型限流（HTTP 429）" + self._body_excerpt(data))
        if status_code >= 500:
            raise _HttpError(
                f"管线模型服务端出错（HTTP {status_code}）" + self._body_excerpt(data)
            )
        if status_code == 400:
            raise _HttpError("管线模型拒绝了请求（HTTP 400）" + self._body_excerpt(data))
        if status_code in (401, 403):
            raise ModelCallError(
                f"管线模型拒绝了请求（HTTP {status_code}）", category="permanent"
            )
        if status_code >= 400:
            raise ModelCallError(
                f"管线模型拒绝了请求（HTTP {status_code}）" + self._body_excerpt(data)
            )
        try:
            content = json.loads(data)["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise _RetryableError("无法解析管线模型的响应") from e
        if not content or not content.strip():
            raise _RetryableError("管线模型返回了空内容")
        return content.strip()

    def _post(self, body: dict, timing: _SendTiming) -> tuple[int, bytes]:
        """Send one chat completion; return its status code and whole body.

        The endpoint gives the send a slot and sends it in its own thread,
        which sets the send's start right before the HTTP call, so that a
        send's time leaves out the wait for the slot and the thread's start.

        httpx's timeout bounds each network operation, e.g. each wait for
        the next bytes, not the whole request: a response that trickles in
        can run far longer than the timeout. So this method waits for the
        send for the request timeout and then cancels it (``_Timeout``);
        the send keeps its slot, and counts as in flight, until its
        connection has closed. ``SendCancelled`` means the client was closed.
        """
        send = self.endpoint.start(
            "/chat/completions",
            body,
            timeout=self.settings.timeout_seconds,
            on_start=self._send_started,
            on_end=self._send_ended,
        )
        timing.slot_wait_seconds = send.slot_wait_seconds
        timing.started_at, timing.sent = send.started_at, send.sent
        try:
            ended = send.wait(self.settings.timeout_seconds)
            if not ended:
                send.cancel()
        finally:
            timing.started_at, timing.sent = send.started_at, send.sent
        if not ended:
            raise _Timeout(self._timeout_reason())
        return send.result()

    def _send_started(self) -> None:
        with self._lock:
            self._in_flight += 1
            self.request_count += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)

    def _send_ended(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def _body_excerpt(self, data: bytes) -> str:
        """``：`` and the start of an error response ``data`` on one line, keys redacted; "" if empty."""
        text = data.decode("utf-8", errors="replace")
        clients = [self] if self.fallback is None else [self, self.fallback]
        for key in {client.settings.api_key for client in clients} - {""}:
            text = text.replace(key, REDACTED)
        text = " ".join(text.split())[:ERROR_BODY_CHARACTERS]
        return f"：{text}" if text else ""

    def _count_timeout(self) -> None:
        with self._lock:
            self.timeout_count += 1

    def _timeout_reason(self) -> str:
        return f"管线模型请求超时（{self.settings.timeout_seconds:g} 秒）"

    def counters(self) -> tuple[int, int]:
        """(requests sent, timeouts) so far."""
        with self._lock:
            return self.request_count, self.timeout_count

    def close(self) -> None:
        """Cancel the endpoint's sends in flight and close it (also for a client sharing it)."""
        self.endpoint.close()


def _result_of(error: _RetryableError) -> str:
    """The record result of a send that failed with ``error``."""
    if isinstance(error, _Timeout):
        return "timeout"
    if isinstance(error, _NetworkError):
        return "network_error"
    if isinstance(error, _HttpError):
        return "http_error"
    return "invalid_answer"


class PipelineModels:
    """The paper task's two clients (ADR 0004).

    ``argon`` takes the paper dossier, the term list and the promoted glosses
    request; ``flash`` takes everything else and forwards to ``argon``. Both
    write their request records to ``request_log``.

    When both have the same address and API key they share one endpoint,
    with one budget of flash's concurrency setting (spec section 7).
    """

    def __init__(self, settings: PipelineSettings, request_log: RequestLog | None = None):
        self.request_log = request_log
        flash, argon = settings.flash, settings.argon
        flash_endpoint = Endpoint(flash)
        argon_endpoint = None
        # Argon's own setting when it is ignored (shared budget), else None.
        self.ignored_argon_concurrency: int | None = None
        if _endpoint_identity(flash) == _endpoint_identity(argon):
            argon_endpoint = flash_endpoint
            self.ignored_argon_concurrency = argon.concurrency
            logger.info(
                "flash and argon have the same address and API key: they share one budget "
                "of %d requests in flight (flash's setting); argon's concurrency setting "
                "(%d) is ignored",
                flash.concurrency,
                argon.concurrency,
            )
        self.argon = ModelClient(argon, role="argon", request_log=request_log, endpoint=argon_endpoint)
        self.flash = ModelClient(
            flash, fallback=self.argon, role="flash", request_log=request_log, endpoint=flash_endpoint
        )

    @property
    def shared_budget(self) -> bool:
        return self.flash.endpoint is self.argon.endpoint

    def endpoints(self) -> dict[str, ModelClient]:
        return {"flash": self.flash, "argon": self.argon}

    def usage(self) -> dict[str, dict[str, int]]:
        """Requests sent and timeouts so far, by model name."""
        usage: dict[str, dict[str, int]] = {}
        for client in self.endpoints().values():
            requests, timeouts = client.counters()
            counts = usage.setdefault(client.model, {"requests": 0, "timeouts": 0})
            counts["requests"] += requests
            counts["timeouts"] += timeouts
        return usage

    def close(self) -> None:
        for client in self.endpoints().values():
            client.close()


def _endpoint_identity(settings: ModelSettings) -> tuple:
    """What makes two settings one endpoint: the address (case of scheme and host,
    default port and a trailing slash aside) and the API key."""
    url = httpx.URL(settings.base_url)
    return (url.scheme, url.host, url.port, url.path.rstrip("/"), settings.api_key)


class ResponseCache:
    """Successful answers on disk, one JSON file per request-body hash."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    @staticmethod
    def key(body: dict) -> str:
        data = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def get(self, key: str) -> str | None:
        entry = self.entry(key)
        return entry["content"] if entry is not None else None

    def entry(self, key: str) -> dict | None:
        """``{"content", "endpoint", "model"}``; entries from before ticket 12 hold only content."""
        try:
            entry = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or not isinstance(entry.get("content"), str):
            return None
        return entry

    def put(
        self, key: str, content: str, endpoint: str | None = None, model: str | None = None
    ) -> None:
        """Save an answer with the endpoint role and model that gave it."""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}-{threading.get_ident()}")
        entry = {"content": content, "endpoint": endpoint, "model": model}
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"
