"""Pipeline model client: every model request of a paper task goes through here.

It calls an OpenAI-compatible chat completions endpoint with the configured
model, reasoning effort, temperature and timeout, and caps the number of
requests in flight with one limit shared by the whole paper task.

Reliability (ADR 0002):

- The request timeout bounds the whole request, from sending it to reading
  the last byte of the response (see ``_post``).
- Network errors, timeouts, HTTP 429 and 5xx, empty content and answers the
  caller's ``validate`` rejects are retried, at most ``max_retries`` times,
  waiting ``retry_backoff_seconds`` and then twice as long each time. Other
  HTTP errors (e.g. 401) fail at once. When the attempts are used up,
  ``complete`` raises ``ModelCallError`` with a readable Chinese reason.
- Successful answers (those ``validate`` accepts) are cached on disk, keyed by
  a hash of the whole request body, which includes the model name. The cache
  directory is shared by all paper tasks, so a resubmitted paper reuses every
  answer that succeeded before.

Two endpoints (ADR 0004): ``PipelineModels`` holds a client for flash and
one for argon. A flash request whose attempts time out twice in a row sends
its remaining attempts to argon (its ``fallback``):

- Only timeouts count; any other failed attempt (HTTP 429 or 5xx, a network
  error, empty content, a rejected answer) sets the count back to 0.
- A forwarded request stays on argon, and its attempts stay ``1 +
  max_retries`` in total. Argon's attempts take a slot of argon's own
  concurrency limit, which all argon requests share.
- A forwarded answer is cached under the flash request, so a resubmitted
  paper finds it without sending anything.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from zotero_translate.settings import ModelSettings
from zotero_translate.settings import PipelineSettings

logger = logging.getLogger(__name__)

Validator = Callable[[str], object]


class ModelCallError(Exception):
    """A model request failed for good; the message is a readable Chinese reason.

    ``answered`` is True when at least one attempt got an answer that the
    caller's ``validate`` rejected, i.e. the endpoint did answer.
    """

    def __init__(self, message: str, answered: bool = False):
        super().__init__(message)
        self.answered = answered


# Consecutive timeouts after which a flash request goes to argon (ADR 0004).
FORWARD_AFTER_TIMEOUTS = 2


class _RetryableError(Exception):
    """One attempt failed in a way that a resend may fix."""


class _Timeout(_RetryableError):
    """One attempt ran past the request timeout."""


class ModelClient:
    """One endpoint's client: its model, its concurrency limit, the cache and the retries.

    ``fallback`` is the client that takes over a request after
    ``FORWARD_AFTER_TIMEOUTS`` timeouts in a row; None never forwards.
    """

    def __init__(self, settings: ModelSettings, fallback: ModelClient | None = None):
        self.settings = settings
        self.fallback = fallback
        self._cache = ResponseCache(settings.cache_dir) if settings.cache_dir else None
        self._slots = threading.BoundedSemaphore(settings.concurrency)
        self._lock = threading.Lock()
        self._in_flight = 0
        self.request_count = 0  # HTTP requests sent to this endpoint, resends included
        self.timeout_count = 0  # of those, the ones that ran past the request timeout
        self.cache_hits = 0
        self.retry_count = 0
        self.max_in_flight = 0
        self.forwarded_pages: list[int | None] = []  # one per request sent on to ``fallback``
        self._http = httpx.Client(
            base_url=settings.base_url,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=settings.timeout_seconds,
            # ``_slots`` caps the requests in flight. The pool is not capped,
            # so a connection that an abandoned request still holds (see
            # ``_post``) never makes a new request wait.
            limits=httpx.Limits(
                max_connections=None,
                max_keepalive_connections=settings.concurrency,
            ),
        )

    @property
    def model(self) -> str:
        return self.settings.model

    def complete(
        self, messages: list[dict], validate: Validator | None = None, page: int | None = None
    ) -> str:
        """Return the stripped message content of one chat completion.

        ``validate(content)`` raises if the content is unusable (e.g. it does
        not parse); such an answer is retried like a failed request and is
        never cached. ``page`` (1-based) is where the request comes from; the
        task report lists it when the request is forwarded.
        """
        body = self._body(messages)
        key = ResponseCache.key(body)
        if self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                with self._lock:
                    self.cache_hits += 1
                return cached
        attempts = 1 + self.settings.max_retries
        answered = False
        target, target_body = self, body
        timeouts_in_a_row = 0
        forwarded_from = None  # the attempt number of the first attempt on the fallback
        for attempt in range(1, attempts + 1):
            try:
                content = target._send(target_body)
                if validate is not None:
                    try:
                        validate(content)
                    except Exception as e:  # noqa: BLE001 - any rejection means "unparseable"
                        answered = True
                        raise _RetryableError(f"无法解析管线模型的响应：{e}") from e
            except _RetryableError as e:
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
                wait = self.settings.retry_backoff_seconds * 2 ** (attempt - 1)
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
            if self._cache is not None:
                self._cache.put(key, content)
            return content
        raise AssertionError("unreachable")

    def _body(self, messages: list[dict]) -> dict:
        return {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "reasoning_effort": self.settings.reasoning_effort,
        }

    def _send(self, body: dict) -> str:
        with self._slots:
            with self._lock:
                self._in_flight += 1
                self.request_count += 1
                self.max_in_flight = max(self.max_in_flight, self._in_flight)
            try:
                status_code, data = self._post(body)
            except _Timeout:
                self._count_timeout()
                raise
            except httpx.ConnectError as e:
                raise _RetryableError("无法连接管线模型") from e
            except httpx.TimeoutException as e:
                self._count_timeout()
                raise _Timeout(self._timeout_reason()) from e
            except httpx.TransportError as e:
                raise _RetryableError(f"与管线模型的网络连接出错：{type(e).__name__}") from e
            finally:
                with self._lock:
                    self._in_flight -= 1
        if status_code == 429:
            raise _RetryableError("管线模型限流（HTTP 429）")
        if status_code >= 500:
            raise _RetryableError(f"管线模型服务端出错（HTTP {status_code}）")
        if status_code >= 400:
            raise ModelCallError(f"管线模型拒绝了请求（HTTP {status_code}）")
        try:
            content = json.loads(data)["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise _RetryableError("无法解析管线模型的响应") from e
        if not content or not content.strip():
            raise _RetryableError("管线模型返回了空内容")
        return content.strip()

    def _post(self, body: dict) -> tuple[int, bytes]:
        """Send one chat completion; return its status code and whole body.

        httpx's timeout bounds each network operation, e.g. each wait for
        the next bytes, not the whole request: a response that trickles in
        can run far longer than the timeout. So the request runs in its own
        thread, and this method stops waiting when the request timeout has
        passed since it started. The abandoned request closes its connection
        at its next read, or at httpx's timeout if no more bytes arrive.
        """
        outcome: dict = {}
        abandoned = threading.Event()

        def run() -> None:
            try:
                with self._http.stream("POST", "/chat/completions", json=body) as response:
                    chunks = []
                    for chunk in response.iter_bytes():
                        if abandoned.is_set():
                            return
                        chunks.append(chunk)
                    outcome["response"] = (response.status_code, b"".join(chunks))
            except Exception as e:  # noqa: BLE001 - re-raised in the caller's thread
                outcome["error"] = e

        request = threading.Thread(target=run, name="model-request", daemon=True)
        request.start()
        request.join(self.settings.timeout_seconds)
        if request.is_alive():
            abandoned.set()
            raise _Timeout(self._timeout_reason())
        if "error" in outcome:
            raise outcome["error"]
        return outcome["response"]

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
        self._http.close()


class PipelineModels:
    """The paper task's two clients (ADR 0004).

    ``argon`` takes the paper dossier, the term list and the promoted glosses
    request; ``flash`` takes everything else and forwards to ``argon``.
    """

    def __init__(self, settings: PipelineSettings):
        self.argon = ModelClient(settings.argon)
        self.flash = ModelClient(settings.flash, fallback=self.argon)

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


class ResponseCache:
    """Successful answers on disk, one JSON file per request-body hash."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    @staticmethod
    def key(body: dict) -> str:
        data = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def get(self, key: str) -> str | None:
        try:
            return json.loads(self._path(key).read_text(encoding="utf-8"))["content"]
        except (OSError, ValueError, KeyError):
            return None

    def put(self, key: str, content: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}-{threading.get_ident()}")
        tmp.write_text(json.dumps({"content": content}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"
