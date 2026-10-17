"""Pipeline model client: every model request of a paper task goes through here.

It calls an OpenAI-compatible chat completions endpoint with the configured
model, reasoning effort, temperature and timeout, and caps the number of
requests in flight with one limit shared by the whole paper task.
"""

from __future__ import annotations

import threading

import httpx

from zotero_translate.settings import ModelSettings


class ModelResponseError(Exception):
    """The pipeline model answered, but the answer is unusable."""


class ModelClient:
    def __init__(self, settings: ModelSettings):
        self.settings = settings
        self._slots = threading.BoundedSemaphore(settings.concurrency)
        self._lock = threading.Lock()
        self._in_flight = 0
        self.request_count = 0
        self.max_in_flight = 0
        self._http = httpx.Client(
            base_url=settings.base_url,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=settings.timeout_seconds,
            limits=httpx.Limits(
                max_connections=settings.concurrency,
                max_keepalive_connections=settings.concurrency,
            ),
        )

    @property
    def model(self) -> str:
        return self.settings.model

    def complete(self, messages: list[dict]) -> str:
        """Send one chat completion request; return the stripped message content."""
        body = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "reasoning_effort": self.settings.reasoning_effort,
        }
        with self._slots:
            with self._lock:
                self._in_flight += 1
                self.request_count += 1
                self.max_in_flight = max(self.max_in_flight, self._in_flight)
            try:
                response = self._http.post("/chat/completions", json=body)
            finally:
                with self._lock:
                    self._in_flight -= 1
        response.raise_for_status()
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ModelResponseError(f"无法解析管线模型的响应：{e}") from e
        if not content or not content.strip():
            raise ModelResponseError("管线模型返回了空内容")
        return content.strip()

    def close(self) -> None:
        self._http.close()
