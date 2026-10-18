"""Task report: what one paper task did, written as JSON when the task ends.

It records the time spent in each stage, the pipeline model client's
counters (HTTP requests sent, cache hits, retries, the maximum number of
requests in flight), the batches whose requests failed for good and the
term list counts with the spellings dropped because the source lacks them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from zotero_translate.model_client import ModelClient


class TaskReport:
    def __init__(self):
        self._started = time.monotonic()
        self._stages: list[list] = []  # [stage, start time, end time or None]
        self.state = "succeeded"
        self.error: str | None = None
        self.failed_batches: list[dict] = []
        self.terms: dict | None = None

    def enter_stage(self, stage: str) -> None:
        if self._stages and self._stages[-1][0] == stage:
            return
        now = time.monotonic()
        if self._stages:
            self._stages[-1][2] = now
        self._stages.append([stage, now, None])

    def fail(self, error: str, failed_batches: list[dict] | None = None) -> None:
        self.state = "failed"
        self.error = error
        self.failed_batches = failed_batches or []

    def write(self, path: Path, client: ModelClient) -> None:
        now = time.monotonic()
        report = {
            "state": self.state,
            "error": self.error,
            "total_seconds": round(now - self._started, 1),
            "stages": [
                {"stage": stage, "seconds": round((end or now) - start, 1)}
                for stage, start, end in self._stages
            ],
            "model": {
                "model": client.model,
                "concurrency": client.settings.concurrency,
                "requests": client.request_count,
                "cache_hits": client.cache_hits,
                "retries": client.retry_count,
                "max_in_flight": client.max_in_flight,
            },
            "failed_batches": self.failed_batches,
            "terms": self.terms,
        }
        Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
