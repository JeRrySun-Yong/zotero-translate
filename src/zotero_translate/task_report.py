"""Task report: what one attempt of a paper task did, written as JSON when it ends.

In the translation service each attempt has its own report, which starts
with the attempt number; the service copies the last one to the task's
``report.json`` with a summary of all attempts (``zotero_translate.service``).
``run`` has one attempt and no attempt number.

It records the time spent in each stage with the requests sent and the
timeouts in it by model name, the pipeline model clients' counters (in
total: HTTP requests sent, cache hits, retries, timeouts; the requests
forwarded from flash to argon with their pages; for each endpoint: model,
concurrency limit, i.e. the size of its endpoint's budget, whether flash and
argon share that budget (same address and key), requests, timeouts and the
maximum number of its requests in flight, and of the budget's), the batches whose requests failed for good and the
term list counts with the spellings dropped because the source lacks them,
the promoted basic terms, the promoted glosses request and the gloss checks,
for each key concept the pages of its first-mention glosses in the
abstract and in the body and whether the translation contains them (with
the planned glosses it does not contain), the review counts (paragraphs the review changed,
with draft and reviewed text; paragraphs that fell back to their draft) and
the paragraphs that kept their source text, with page and reason, and the
algorithms (pseudocode) found: pages, numbered lines, result ("translated" or
"kept_source") and the reason for keeping the source, each translated row's
source and final text, and each line's number, start x and glyphs, and the
translated text the mono PDF draws but does not show (``lost_text``: page
and text of each piece; see ``zotero_translate.lost_text``), and one record
per model request send and per cache hit (``requests``; see
``zotero_translate.request_log``, which appends them to ``requests.jsonl``
next to the report as they happen).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from zotero_translate.algorithms import AlgorithmDetector
from zotero_translate.model_client import PipelineModels
from zotero_translate.review import BatchReviewer
from zotero_translate.review import KeptSource


class TaskReport:
    def __init__(
        self,
        models: PipelineModels,
        kept: KeptSource,
        algorithms: AlgorithmDetector | None = None,
        attempt: int | None = None,
    ):
        self.attempt = attempt
        self.models = models
        self.kept = kept
        self.algorithms = algorithms or AlgorithmDetector()
        self.reviewer: BatchReviewer | None = None
        self._started = time.monotonic()
        # [stage, start time, end time or None, usage at start, usage at end or None]
        self._stages: list[list] = []
        self.state = "succeeded"
        self.error: str | None = None
        self.failure_category: str | None = None  # "permanent" or "retryable" once failed
        self.failed_batches: list[dict] = []
        self.terms: dict | None = None
        self.first_mentions: list[dict] | None = None
        self.first_mention_glosses: dict | None = None
        # Pieces of translated text the mono PDF draws but does not show, with
        # their pages (closure ticket 15, ``zotero_translate.lost_text``); None
        # until the mono PDF is written.
        self.lost_text: list[dict] | None = None

    def enter_stage(self, stage: str) -> None:
        if self.models.request_log is not None:
            self.models.request_log.stage = stage
        if self._stages and self._stages[-1][0] == stage:
            return
        now = time.monotonic()
        usage = self.models.usage()
        if self._stages:
            self._stages[-1][2] = now
            self._stages[-1][4] = usage
        self._stages.append([stage, now, None, usage, None])

    def fail(
        self, error: str, failed_batches: list[dict] | None = None, category: str = "retryable"
    ) -> None:
        self.state = "failed"
        self.error = error
        self.failure_category = category
        self.failed_batches = failed_batches or []

    def write(self, path: Path) -> None:
        now = time.monotonic()
        usage_now = self.models.usage()
        endpoints = self.models.endpoints()
        clients = endpoints.values()
        forwarded = self.models.flash.forwarded_pages
        report = {
            **({"attempt": self.attempt} if self.attempt is not None else {}),
            "state": self.state,
            "error": self.error,
            "failure_category": self.failure_category,
            "total_seconds": round(now - self._started, 1),
            "stages": [
                {
                    "stage": stage,
                    "seconds": round((end or now) - start, 1),
                    "models": _usage_between(before, after or usage_now),
                }
                for stage, start, end, before, after in self._stages
            ],
            "model": {
                "requests": sum(c.request_count for c in clients),
                "cache_hits": sum(c.cache_hits for c in clients),
                "retries": sum(c.retry_count for c in clients),
                "timeouts": sum(c.timeout_count for c in clients),
                "forwarded": {
                    "requests": len(forwarded),
                    "pages": sorted(forwarded, key=lambda p: (p is None, p)),
                },
                "endpoints": {
                    name: {
                        "model": c.model,
                        "concurrency": c.concurrency,
                        "shared_budget": self.models.shared_budget,
                        "requests": c.request_count,
                        "timeouts": c.timeout_count,
                        "max_in_flight": c.max_in_flight,
                        "budget_max_in_flight": c.endpoint.max_in_flight,
                    }
                    for name, c in endpoints.items()
                },
            },
            "failed_batches": self.failed_batches,
            "terms": self.terms,
            "first_mentions": self.first_mentions,
            "first_mention_glosses": self.first_mention_glosses,
            "review": self.reviewer.report() if self.reviewer else None,
            "kept_source_paragraphs": list(self.kept.paragraphs),
            "kept_source_algorithms": self.algorithms.kept_source,
            "algorithms": self.algorithms.report(),
            "lost_text": self.lost_text,
            "requests": (
                list(self.models.request_log.records) if self.models.request_log else []
            ),
        }
        Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _usage_between(before: dict, after: dict) -> dict:
    """``{model: {"requests", "timeouts"}}`` sent between two usage snapshots; idle models left out."""
    usage = {}
    for model, counts in after.items():
        start = before.get(model, {"requests": 0, "timeouts": 0})
        delta = {name: counts[name] - start[name] for name in ("requests", "timeouts")}
        if any(delta.values()):
            usage[model] = delta
    return usage
