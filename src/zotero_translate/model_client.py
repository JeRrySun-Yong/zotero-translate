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

logger = logging.getLogger(__name__)

Validator = Callable[[str], object]


class ModelCallError(Exception):
    """A model request failed for good; the message is a readable Chinese reason."""


class _RetryableError(Exception):
    """One attempt failed in a way that a resend may fix."""


class ModelClient:
    def __init__(self, settings: ModelSettings):
        self.settings = settings
        self._cache = ResponseCache(settings.cache_dir) if settings.cache_dir else None
        self._slots = threading.BoundedSemaphore(settings.concurrency)
        self._lock = threading.Lock()
        self._in_flight = 0
        self.request_count = 0  # HTTP requests sent, resends included
        self.cache_hits = 0
        self.retry_count = 0
        self.max_in_flight = 0
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

    def complete(self, messages: list[dict], validate: Validator | None = None) -> str:
        """Return the stripped message content of one chat completion.

        ``validate(content)`` raises if the content is unusable (e.g. it does
        not parse); such an answer is retried like a failed request and is
        never cached.
        """
        body = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "reasoning_effort": self.settings.reasoning_effort,
        }
        key = ResponseCache.key(body)
        if self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                with self._lock:
                    self.cache_hits += 1
                return cached
        attempts = 1 + self.settings.max_retries
        for attempt in range(1, attempts + 1):
            try:
                content = self._send(body)
                if validate is not None:
                    try:
                        validate(content)
                    except Exception as e:  # noqa: BLE001 - any rejection means "unparseable"
                        raise _RetryableError(f"无法解析管线模型的响应：{e}") from e
            except _RetryableError as e:
                if attempt == attempts:
                    raise ModelCallError(f"{e}（共尝试 {attempts} 次）") from e
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

    def _send(self, body: dict) -> str:
        with self._slots:
            with self._lock:
                self._in_flight += 1
                self.request_count += 1
                self.max_in_flight = max(self.max_in_flight, self._in_flight)
            try:
                status_code, data = self._post(body)
            except httpx.ConnectError as e:
                raise _RetryableError("无法连接管线模型") from e
            except httpx.TimeoutException as e:
                raise _RetryableError(self._timeout_reason()) from e
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
            raise _RetryableError(self._timeout_reason())
        if "error" in outcome:
            raise outcome["error"]
        return outcome["response"]

    def _timeout_reason(self) -> str:
        return f"管线模型请求超时（{self.settings.timeout_seconds:g} 秒）"

    def close(self) -> None:
        self._http.close()


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
