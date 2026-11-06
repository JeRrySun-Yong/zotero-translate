"""Hedging (M3 ticket 15; spec section 8, seam 2; ADR 0004).

A flash send of a translation batch or a review that has run the hedging
threshold with no end gets one copy sent to argon; the first answer that
passes the caller's check wins and the other send is cancelled. Each test
names the row of the decision record's schedule (ticket 14, 「调度表草案」 and
its last comment) that it checks. Two fake models stand for flash and argon;
the threshold is ``HEDGE`` seconds instead of 400.

These tests drive the pipeline model client; none starts the service or a
worker (the service's switch is in ``test_hedging_service.py``).
"""

import json
import logging
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import FAKE_API_KEY
from tests.conftest import FAKE_ARGON_API_KEY
from tests.fake_model import FAKE_DOSSIER
from tests.fake_model import FakeModel
from zotero_translate.model_client import REDACTED
from zotero_translate.model_client import ModelCallError
from zotero_translate.model_client import PipelineModels
from zotero_translate.model_client import ResponseCache
from zotero_translate.request_log import REQUESTS_FILE
from zotero_translate.request_log import RequestLog
from zotero_translate.request_log import load_records
from zotero_translate.review import BatchReviewer
from zotero_translate.review import KeptSource
from zotero_translate.settings import ModelSettings
from zotero_translate.settings import PipelineSettings

HEDGE = 0.5  # seconds; production uses 400
MESSAGES = [{"role": "user", "content": "hello"}]


def _settings(fake: FakeModel, tmp_path: Path, **changes) -> ModelSettings:
    values = dict(
        base_url=fake.base_url,
        api_key=FAKE_API_KEY,
        model="fake-flash",
        concurrency=4,
        timeout_seconds=5.0,
        retry_backoff_seconds=0.01,
        connection_retry_seconds=(0.01, 0.01, 0.01),
        cache_dir=tmp_path / "cache",
    )
    values.update(changes)
    return ModelSettings(**values)


def _models(
    flash: FakeModel,
    argon: FakeModel,
    tmp_path: Path,
    hedge_after: float | None = HEDGE,
    flash_changes: dict | None = None,
    argon_changes: dict | None = None,
) -> tuple[PipelineModels, RequestLog]:
    log = RequestLog(tmp_path / REQUESTS_FILE, attempt=1)
    log.stage = "翻译与审校"
    settings = PipelineSettings(
        flash=_settings(flash, tmp_path, **(flash_changes or {})),
        argon=_settings(
            argon, tmp_path, model="fake-argon", api_key=FAKE_ARGON_API_KEY, **(argon_changes or {})
        ),
        hedge_after_seconds=hedge_after,
    )
    return PipelineModels(settings, log), log


def _later(seconds: float, content: str = "ok"):
    """A responder that answers ``content`` after ``seconds``."""

    def respond(body: dict) -> str:
        time.sleep(seconds)
        return content

    return respond


def _records(log: RequestLog, count: int, timeout: float = 10.0) -> list[dict]:
    """The records once there are ``count`` (a loser's comes after the call returns), by send."""
    deadline = time.monotonic() + timeout
    while len(load_records(log.path)) < count and time.monotonic() < deadline:
        time.sleep(0.02)
    records = load_records(log.path)
    assert len(records) == count, records
    return sorted(records, key=lambda r: (r["request"], r["send"] or 0))


def _summary(records: list[dict]) -> list[tuple]:
    return [(r["send"], r["endpoint"], r["result"], r["hedge"]) for r in records]


def _wait_until(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


# 「对冲哪些请求、阈值」 and 「胜者出现后」 (the loser is cancelled at once).
@pytest.mark.parametrize("kind", ["translation_batch", "review"])
def test_a_slow_flash_send_gets_an_argon_copy_that_wins_and_the_original_is_cancelled(
    tmp_path, kind
):
    with FakeModel() as flash, FakeModel() as argon:
        flash.inject_error("timeout")  # no answer until the client disconnects
        models, log = _models(flash, argon, tmp_path)
        try:
            assert models.flash.complete(MESSAGES, page=3, kind=kind) == "ok"
            # The original's connection is closed: the fake sees the disconnect.
            assert _wait_until(lambda: flash.requests[0].disconnected)
            records = _records(log, 2)
            # The same request again: argon's answer, cached under flash's request.
            assert models.flash.complete(MESSAGES, page=3, kind=kind) == "ok"
        finally:
            models.close()

    assert _summary(records) == [
        (1, "flash", "cancelled_for_winner", False),
        (2, "argon", "succeeded", True),
    ]
    assert [r["kind"] for r in records] == [kind, kind]
    assert [r["model"] for r in records] == ["fake-flash", "fake-argon"]
    [original], [copy] = flash.requests, argon.requests
    assert copy.body["model"] == "fake-argon"
    assert HEDGE * 0.9 <= copy.received_at - original.received_at < HEDGE + 1.0
    [hit] = load_records(log.path)[2:]
    assert (hit["result"], hit["endpoint"], hit["hedge"]) == ("cache_hit", "argon", False)
    key = ResponseCache.key(models.flash._body(MESSAGES))
    assert ResponseCache(tmp_path / "cache").entry(key) == {
        "content": "ok",
        "endpoint": "argon",
        "model": "fake-argon",
    }
    # The copy took a slot of argon's budget, not flash's.
    assert models.flash.endpoint.max_in_flight == 1
    assert models.argon.endpoint.max_in_flight == 1
    assert models.flash.hedge_count == 1


# 「对冲哪些请求、阈值」: an answer before the threshold gets no copy.
def test_a_flash_answer_before_the_threshold_gets_no_copy(tmp_path):
    with FakeModel() as flash, FakeModel() as argon:
        flash.respond("other", _later(HEDGE / 3))
        models, log = _models(flash, argon, tmp_path)
        try:
            assert models.flash.complete(MESSAGES, kind="translation_batch") == "ok"
        finally:
            models.close()

    assert argon.requests == []
    assert _summary(load_records(log.path)) == [(1, "flash", "succeeded", False)]


# 「对冲哪些请求、阈值」: the other request kinds are not hedged.
@pytest.mark.parametrize(
    "kind", ["algorithm_translation", "algorithm_review", "single_paragraph", "gloss_rewrite"]
)
def test_other_request_kinds_on_flash_get_no_copy(tmp_path, kind):
    with FakeModel() as flash, FakeModel() as argon:
        flash.respond("other", _later(HEDGE * 2.5))
        models, log = _models(flash, argon, tmp_path)
        try:
            assert models.flash.complete(MESSAGES, kind=kind) == "ok"
        finally:
            models.close()

    assert argon.requests == []
    assert _summary(load_records(log.path)) == [(1, "flash", "succeeded", False)]


# 「副模型处理的请求（论文档案、术语清单）是否对冲」: no.
@pytest.mark.parametrize("kind", ["dossier", "term_list"])
def test_the_dossier_and_term_list_on_argon_get_no_copy(tmp_path, kind):
    with FakeModel() as flash, FakeModel() as argon:
        argon.respond("other", _later(HEDGE * 2.5))
        models, log = _models(flash, argon, tmp_path)
        try:
            assert models.argon.complete(MESSAGES, kind=kind) == "ok"
        finally:
            models.close()

    assert flash.requests == [] and len(argon.requests) == 1
    assert _summary(load_records(log.path)) == [(1, "argon", "succeeded", False)]


# 「单模型配置是否对冲」: no, when both roles are one endpoint.
def test_a_single_model_configuration_gets_no_copy(tmp_path):
    with FakeModel() as model:
        model.respond("other", _later(HEDGE * 2.5))
        log = RequestLog(tmp_path / REQUESTS_FILE, attempt=1)
        settings = PipelineSettings(
            flash=_settings(model, tmp_path),
            argon=_settings(model, tmp_path),  # same address and key: one endpoint
            hedge_after_seconds=HEDGE,
        )
        models = PipelineModels(settings, log)
        try:
            assert models.shared_budget
            assert models.flash.complete(MESSAGES, kind="translation_batch") == "ok"
        finally:
            models.close()

    assert len(model.requests) == 1
    assert _summary(load_records(log.path)) == [(1, "flash", "succeeded", False)]


# The switch: off, no copy, and a request still goes to argon after 2 timeouts.
def test_hedging_off_sends_no_copy_and_forwards_after_two_timeouts(tmp_path):
    with FakeModel() as flash, FakeModel() as argon:
        flash.fail_same_request(["timeout", "timeout"])
        models, log = _models(
            flash, argon, tmp_path, hedge_after=None, flash_changes={"timeout_seconds": 1.0}
        )
        try:
            assert models.flash.complete(MESSAGES, kind="translation_batch") == "ok"
        finally:
            models.close()

    assert _summary(load_records(log.path)) == [
        (1, "flash", "timeout", False),
        (2, "flash", "timeout", False),
        (3, "argon", "succeeded", False),
    ]
    assert models.flash.hedge_count == 0


# 「一个逻辑请求最多同时几份发送」(2), 「与连续 2 次超时后改发 argon 的关系」 (after a
# flash timeout the next send goes straight to argon) and 「重发之后还能不能再对冲」
# (an argon send gets no copy).
def test_two_sends_at_most_and_after_a_flash_timeout_the_rest_go_to_argon_unhedged(tmp_path):
    with FakeModel(slow_body_seconds=HEDGE * 3) as flash, FakeModel(
        slow_body_seconds=HEDGE * 3
    ) as argon:
        flash.inject_error("timeout")
        argon.fail_same_request(["timeout", "slow"])
        models, log = _models(
            flash,
            argon,
            tmp_path,
            flash_changes={"timeout_seconds": 1.5},
            argon_changes={"timeout_seconds": 2.0},
        )
        try:
            assert models.flash.complete(MESSAGES, page=5, kind="translation_batch")
        finally:
            models.close()

    records = _records(log, 3)
    assert _summary(records) == [
        (1, "flash", "timeout", False),
        (2, "argon", "timeout", True),
        (3, "argon", "succeeded", False),  # slow past the threshold, and no copy
    ]
    assert len(flash.requests) == 1 and len(argon.requests) == 2
    # Never more than 2 sends of the request in flight at once.
    spans = [(r["started_at"], r["started_at"] + r["seconds"]) for r in records]
    for start, _ in spans:
        assert sum(1 for s, e in spans if s <= start < e) <= 2
    # The third send went out only once both earlier sends had ended.
    assert spans[2][0] >= max(spans[0][1], spans[1][1]) - 0.05
    assert models.flash.forwarded_pages == [5]


# 「重发之后还能不能再对冲」: a resend on flash is hedged again.
def test_a_flash_resend_gets_a_copy_too(tmp_path):
    with FakeModel() as flash, FakeModel() as argon:
        flash.fail_same_request(["500", "timeout"])
        models, log = _models(flash, argon, tmp_path)
        try:
            assert models.flash.complete(MESSAGES, kind="review") == "ok"
        finally:
            models.close()

    assert _summary(_records(log, 3)) == [
        (1, "flash", "http_error", False),
        (2, "flash", "cancelled_for_winner", False),
        (3, "argon", "succeeded", True),
    ]


# 「所有发送（含副本）合计不超过 4 次」: a fourth send gets no copy.
def test_a_fourth_send_gets_no_copy_and_the_request_fails_after_four(tmp_path):
    with FakeModel() as flash, FakeModel() as argon:
        flash.fail_same_request(["500", "500", "500", "timeout"])
        models, log = _models(flash, argon, tmp_path, flash_changes={"timeout_seconds": 1.5})
        try:
            with pytest.raises(ModelCallError) as failure:
                models.flash.complete(MESSAGES, kind="translation_batch")
        finally:
            models.close()

    assert argon.requests == []
    assert [r["result"] for r in load_records(log.path)] == ["http_error"] * 3 + ["timeout"]
    assert "管线模型请求超时" in str(failure.value) and "共尝试 4 次" in str(failure.value)
    assert failure.value.category == "retryable"


# 「到了阈值但没有额度」: the copy waits for an argon slot, never takes flash's,
# and is not sent once the original has ended (failed or succeeded).
def test_a_copy_waiting_for_an_argon_slot_is_not_sent_once_the_original_has_ended(tmp_path):
    with FakeModel(delay=HEDGE * 2) as flash, FakeModel() as argon:
        flash.fail_same_request(["500"])  # the first send fails after the threshold
        argon.respond("other", _later(HEDGE * 8))
        models, log = _models(flash, argon, tmp_path, argon_changes={"concurrency": 1})
        blocker = threading.Thread(
            target=models.argon.complete,
            args=([{"role": "user", "content": "blocker"}],),
            kwargs={"kind": "dossier"},
        )
        try:
            blocker.start()  # holds argon's only slot for the whole test
            assert _wait_until(lambda: len(argon.requests) == 1)
            assert models.flash.complete(MESSAGES, kind="translation_batch") == "ok"
            blocker.join()
        finally:
            models.close()

    assert len(argon.requests) == 1  # only the blocker: neither copy was sent
    flash_records = [r for r in load_records(log.path) if r["endpoint"] == "flash"]
    assert _summary(flash_records) == [
        (1, "flash", "http_error", False),
        (2, "flash", "succeeded", False),
    ]
    assert models.flash.hedge_count == 0
    assert models.flash.endpoint.max_in_flight == 1


# 「错误的优先级」: the first answer that passes the check wins; a copy that
# answers first but is rejected does not.
def test_the_first_valid_answer_wins_and_an_earlier_rejected_one_does_not(tmp_path):
    with FakeModel() as flash, FakeModel() as argon:
        flash.respond("other", _later(HEDGE * 2, "good"))
        argon.respond("other", lambda body: "bad")
        models, log = _models(flash, argon, tmp_path)

        def validate(content: str) -> None:
            if content != "good":
                raise ValueError(content)

        try:
            assert models.flash.complete(MESSAGES, validate=validate, kind="review") == "good"
        finally:
            models.close()

    assert _summary(_records(log, 2)) == [
        (1, "flash", "succeeded", False),
        (2, "argon", "invalid_answer", True),
    ]
    key = ResponseCache.key(models.flash._body(MESSAGES))
    assert ResponseCache(tmp_path / "cache").entry(key)["content"] == "good"


# 「错误的优先级」: when every send fails, a credential error comes first.
def test_a_credential_error_is_the_error_when_every_send_fails(tmp_path):
    with FakeModel() as flash, FakeModel() as argon:
        flash.inject_error("timeout")
        argon.inject_error("401")
        models, log = _models(flash, argon, tmp_path, flash_changes={"timeout_seconds": 1.5})
        try:
            with pytest.raises(ModelCallError) as failure:
                models.flash.complete(MESSAGES, kind="translation_batch")
        finally:
            models.close()

    assert failure.value.category == "permanent"
    assert "HTTP 401" in str(failure.value)
    # No send after the credential error; the flash send ran to its timeout.
    assert _summary(_records(log, 2)) == [
        (1, "flash", "timeout", False),
        (2, "argon", "http_error", True),
    ]


# 「错误的优先级」 and spec section 8: when every send is rejected, the
# rejections are in send order, not in the order the answers arrived.
def test_rejections_are_in_send_order_when_every_send_is_rejected(tmp_path):
    with FakeModel() as flash, FakeModel() as argon:
        flash.respond("other", _later(HEDGE * 2, "flash answer"))
        argon.respond("other", lambda body: "argon answer")  # arrives first
        models, _ = _models(
            flash, argon, tmp_path, flash_changes={"max_retries": 1}, argon_changes={"max_retries": 1}
        )

        def validate(content: str) -> None:
            raise ValueError(content)

        try:
            with pytest.raises(ModelCallError) as failure:
                models.flash.complete(MESSAGES, validate=validate, kind="review")
        finally:
            models.close()

    assert failure.value.answered
    assert [str(r) for r in failure.value.rejections] == ["flash answer", "argon answer"]
    assert "共尝试 2 次，其中 1 次发给 fake-argon" in str(failure.value)


# M4 ticket 16 item 1: a check's error that holds an answer echoing the key.
# Each endpoint's answer echoes the key it was sent; both read "[redacted]".
def test_a_hedged_request_rejected_with_the_keys_echoed_reads_redacted_everywhere(
    tmp_path, caplog
):
    with FakeModel() as flash, FakeModel() as argon:
        flash.respond("other", _later(HEDGE * 2, f"flash {FAKE_API_KEY}"))
        argon.respond("other", lambda body: f"argon {FAKE_ARGON_API_KEY}")
        models, log = _models(
            flash, argon, tmp_path, flash_changes={"max_retries": 1}, argon_changes={"max_retries": 1}
        )

        def validate(content: str) -> None:
            raise ValueError(f"术语条目 {content} 缺少判断依据 reason")

        try:
            with caplog.at_level(logging.WARNING):
                with pytest.raises(ModelCallError) as failure:
                    models.flash.complete(MESSAGES, validate=validate, kind="review")
        finally:
            models.close()

    error = failure.value
    keys = (FAKE_API_KEY, FAKE_ARGON_API_KEY)
    assert error.answered
    assert [str(r) for r in error.rejections] == [
        f"术语条目 flash {REDACTED} 缺少判断依据 reason",
        f"术语条目 argon {REDACTED} 缺少判断依据 reason",
    ]
    assert all(isinstance(r, ValueError) for r in error.rejections)
    for text in (str(error), str(error.__cause__), caplog.text, log.path.read_text("utf-8")):
        assert all(key not in text for key in keys), text
    assert REDACTED in str(error)


def test_a_hedged_request_resent_after_a_rejection_with_the_key_echoed_logs_it_redacted(
    tmp_path, caplog
):
    with FakeModel() as flash, FakeModel() as argon:
        flash.respond("other", lambda body: f"flash {FAKE_API_KEY}")  # before the threshold
        models, _ = _models(flash, argon, tmp_path, flash_changes={"max_retries": 1})

        def validate(content: str) -> None:
            raise ValueError(f"术语条目 {content} 缺少判断依据 reason")

        try:
            with caplog.at_level(logging.WARNING):
                with pytest.raises(ModelCallError) as failure:
                    models.flash.complete(MESSAGES, validate=validate, kind="review")
        finally:
            models.close()

    [warning] = [r for r in caplog.messages if "model request failed (attempt 1/2)" in r]
    assert f"术语条目 flash {REDACTED} 缺少判断依据 reason" in warning, warning
    assert FAKE_API_KEY not in caplog.text and FAKE_API_KEY not in str(failure.value)
    assert argon.requests == []


def test_the_parts_of_a_hedged_answer_a_rejection_keeps_are_scrubbed_of_both_keys(tmp_path):
    class Rejection(ValueError):
        def __init__(self, parts: dict):
            super().__init__("还有条目不合格")
            self.parts = parts

    with FakeModel() as flash, FakeModel() as argon:
        flash.respond("other", _later(HEDGE * 2, f"flash {FAKE_API_KEY}"))
        argon.respond("other", lambda body: f"argon {FAKE_ARGON_API_KEY}")
        models, _ = _models(
            flash, argon, tmp_path, flash_changes={"max_retries": 1}, argon_changes={"max_retries": 1}
        )

        def validate(content: str) -> None:
            raise Rejection({"answer": [content]})

        try:
            with pytest.raises(ModelCallError) as failure:
                models.flash.complete(MESSAGES, validate=validate, kind="review")
        finally:
            models.close()

    assert [r.parts for r in failure.value.rejections] == [
        {"answer": [f"flash {REDACTED}"]},
        {"answer": [f"argon {REDACTED}"]},
    ]


# Spec section 8: when every candidate fails, the review falls back as it
# would without hedging (on the verdicts of the last parsed answer by send).
REVIEW_ITEMS = [  # both drafts fail the number check
    {"id": 0, "source": "It has 3 parts.", "draft": "它有几个部分。", "page": 2},
    {"id": 1, "source": "It runs 7 steps.", "draft": "它运行几步。", "page": 2},
]
# Each answer parses and fails the number check on one item: rejected.
FIRST_ANSWER = [{"id": 0, "output": "它有几个部分。"}, {"id": 1, "output": "它共运行 7 步。"}]
SECOND_ANSWER = [{"id": 0, "output": "它分 3 个部分。"}, {"id": 1, "output": "它运行几步。"}]


def _review(models: PipelineModels) -> dict:
    reviewer = BatchReviewer(models.flash, FAKE_DOSSIER, KeptSource())
    return reviewer(REVIEW_ITEMS, [])


def test_a_review_whose_candidates_all_fail_falls_back_as_without_hedging(tmp_path):
    first, second = (json.dumps(a, ensure_ascii=False) for a in (FIRST_ANSWER, SECOND_ANSWER))
    retries = {"max_retries": 1}
    # Hedged: flash's first send answers late; argon's copy, send 2, answers first.
    with FakeModel() as flash, FakeModel() as argon:
        flash.respond("review", _later(HEDGE * 2, first))
        argon.respond("review", lambda body: second)
        models, log = _models(
            flash, argon, tmp_path / "on", flash_changes=retries, argon_changes=retries
        )
        try:
            hedged = _review(models)
        finally:
            models.close()
    assert _summary(_records(log, 2)) == [
        (1, "flash", "invalid_answer", False),
        (2, "argon", "invalid_answer", True),
    ]
    # Not hedged: the same answers by send number, both from flash.
    with FakeModel() as flash, FakeModel() as argon:
        answers = iter([first, second])
        flash.respond("review", lambda body: next(answers))
        models, _ = _models(
            flash, argon, tmp_path / "off", hedge_after=None, flash_changes=retries
        )
        try:
            unhedged = _review(models)
        finally:
            models.close()

    # The second answer's verdicts: its item 0, and the source for item 1.
    assert hedged == unhedged == {0: "它分 3 个部分。", 1: None}


# Spec section 8: 「缓存只写一次，写在原请求的键下，败者不能写，也不能覆盖」, and the
# winner stands even when the loser's end reaches the coordinator first.
def test_a_valid_answer_after_the_winner_loses_and_cannot_overwrite_the_cache(tmp_path):
    with FakeModel() as flash, FakeModel() as argon:
        flash.respond("other", _later(HEDGE + 0.4, "flash"))
        argon.respond("other", _later(0.1, "argon"))
        models, log = _models(flash, argon, tmp_path)
        append = log.append

        def slow_winner_record(**fields) -> None:
            # The winner's record is slow to write: the loser ends meanwhile,
            # before the coordinator hears of the winner and cancels it.
            if fields["result"] == "succeeded":
                time.sleep(1.0)
            append(**fields)

        log.append = slow_winner_record
        try:
            assert models.flash.complete(MESSAGES, kind="translation_batch") == "argon"
        finally:
            models.close()

    assert _summary(_records(log, 2)) == [
        (1, "flash", "cancelled_for_winner", False),
        (2, "argon", "succeeded", True),
    ]
    key = ResponseCache.key(models.flash._body(MESSAGES))
    assert ResponseCache(tmp_path / "cache").entry(key)["content"] == "argon"


# 「错误的优先级」: a credential error after a rejected answer still fails the
# request as a credential error (``answered`` false), as without hedging.
def test_a_credential_error_after_a_rejected_copy_is_not_taken_for_an_answer(tmp_path):
    with FakeModel(delay=HEDGE * 2) as flash, FakeModel() as argon:
        flash.inject_error("401")
        argon.respond("other", lambda body: "bad")
        models, _ = _models(flash, argon, tmp_path)

        def validate(content: str) -> None:
            raise ValueError(content)

        try:
            with pytest.raises(ModelCallError) as failure:
                models.flash.complete(MESSAGES, validate=validate, kind="review")
        finally:
            models.close()

    assert failure.value.category == "permanent"
    assert not failure.value.answered


# Independent acceptance, round 1: the off path (and every request that is
# not hedged) keeps the rejections when a later send fails with an HTTP error
# no resend fixes, so the gloss rewrite keeps the glosses that passed, as
# before ticket 15.
def test_an_unhedged_request_keeps_its_rejections_when_a_401_follows(tmp_path):
    with FakeModel() as argon_fake, FakeModel() as flash:
        argon_fake.inject_error("401", nth=2)
        models, _ = _models(flash, argon_fake, tmp_path)

        def validate(content: str) -> None:
            raise ValueError("first answer rejected")

        try:
            with pytest.raises(ModelCallError) as failure:
                models.argon.complete(MESSAGES, validate=validate, kind="gloss_rewrite")
        finally:
            models.close()

    assert failure.value.category == "permanent"
    assert not failure.value.answered  # as before: the 401 fails the request
    assert [str(r) for r in failure.value.rejections] == ["first answer rejected"]


# Independent acceptance, round 1: 「胜者出现后立即中止败者」: the loser is
# cancelled as the winner is chosen, not after the winner's record is written.
def test_the_loser_is_cancelled_before_the_winners_record_is_written(tmp_path):
    with FakeModel() as flash, FakeModel() as argon:
        flash.inject_error("timeout")  # no answer until the client disconnects
        models, log = _models(flash, argon, tmp_path)
        append = log.append

        def slow_winner_record(**fields) -> None:
            if fields["result"] == "succeeded":
                time.sleep(2.0)
            append(**fields)

        log.append = slow_winner_record
        caller = threading.Thread(
            target=models.flash.complete, args=(MESSAGES,), kwargs={"kind": "translation_batch"}
        )
        try:
            caller.start()
            assert _wait_until(lambda: len(argon.requests) == 1)
            # Well before the winner's record (2 s) is written.
            assert _wait_until(lambda: flash.requests[0].disconnected, timeout=1.0)
            caller.join()
        finally:
            models.close()


# Independent acceptance, round 2: spec section 7, 「每次模型请求发送记一条」:
# a loser's record is written after the request returned; a worker or
# ``run`` that exits right then (``process_exit``) still writes it.
LOSER_RECORD_AT_EXIT = """
import sys, time
from pathlib import Path
from zotero_translate import process_exit
from zotero_translate.model_client import PipelineModels
from zotero_translate.request_log import RequestLog
from zotero_translate.settings import ModelSettings, PipelineSettings

flash_url, argon_url, directory = sys.argv[1:4]
log = RequestLog(Path(directory) / "requests.jsonl", attempt=1)
append = log.append

def slow_loser_record(**fields):
    if fields["result"] == "cancelled_for_winner":
        time.sleep(1.0)
    append(**fields)

log.append = slow_loser_record
settings = PipelineSettings(
    flash=ModelSettings(base_url=flash_url, api_key="flash-key", model="fake-flash", timeout_seconds=30),
    argon=ModelSettings(base_url=argon_url, api_key="argon-key", model="fake-argon", timeout_seconds=30),
    hedge_after_seconds=%r,
)
models = PipelineModels(settings, log)
models.flash.complete([{"role": "user", "content": "hello"}], kind="translation_batch")
process_exit.run_then_exit(lambda: 0)  # at once, as the worker does after its last report
"""


def test_a_losers_record_is_written_even_when_the_process_exits_right_after(tmp_path):
    import subprocess
    import sys

    with FakeModel() as flash, FakeModel() as argon:
        flash.inject_error("timeout")
        done = subprocess.run(
            [sys.executable, "-c", LOSER_RECORD_AT_EXIT % HEDGE, flash.base_url, argon.base_url, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert done.returncode == 0, done.stderr
    records = sorted(load_records(tmp_path / REQUESTS_FILE), key=lambda r: r["send"])
    assert _summary(records) == [
        (1, "flash", "cancelled_for_winner", False),
        (2, "argon", "succeeded", True),
    ]


# Batch acceptance: a send whose record cannot be written still ends: the
# request fails with that error, as it does without hedging, instead of
# waiting for an end that never comes.
@pytest.mark.parametrize("fault", [None, "500"], ids=["answered", "failed"])
def test_a_record_that_cannot_be_written_fails_the_request_instead_of_hanging(tmp_path, fault):
    with FakeModel() as flash, FakeModel() as argon:
        if fault:
            flash.inject_error(fault)
        models, log = _models(flash, argon, tmp_path)

        def broken_append(**fields) -> None:
            raise OSError("disk full")

        log.append = broken_append
        outcome: list = []

        def call() -> None:
            try:
                outcome.append(models.flash.complete(MESSAGES, kind="translation_batch"))
            except BaseException as e:  # noqa: BLE001 - the test looks at it
                outcome.append(e)

        caller = threading.Thread(target=call, daemon=True)
        try:
            caller.start()
            caller.join(5.0)
            assert not caller.is_alive(), "the request never ended"
        finally:
            models.close()

    [error] = outcome
    assert isinstance(error, OSError) and str(error) == "disk full"


# Batch acceptance: 「胜者出现后，还没发出去的副本不再发」 also when the copy
# has its argon slot but its HTTP thread has not sent anything yet.
def test_a_copy_with_a_slot_but_not_yet_sent_is_not_sent_once_the_original_wins(tmp_path):
    with FakeModel() as flash, FakeModel() as argon:
        answered = threading.Event()

        def answer_after_the_threshold(body: dict) -> str:
            time.sleep(HEDGE + 0.3)
            answered.set()
            return "flash"

        flash.respond("other", answer_after_the_threshold)
        models, log = _models(flash, argon, tmp_path)
        endpoint = models.argon.endpoint
        run, start = endpoint._run, endpoint.start

        def slow_thread(*args) -> None:
            # The copy's HTTP thread is slow to start: it sends only after
            # the original has won.
            answered.wait(10)
            time.sleep(0.3)
            run(*args)

        def slow_return(*args, **kwargs):
            # The caller is slower still to get the send back from ``start``.
            send = start(*args, **kwargs)
            answered.wait(10)
            time.sleep(0.8)
            return send

        endpoint._run, endpoint.start = slow_thread, slow_return
        try:
            assert models.flash.complete(MESSAGES, kind="translation_batch") == "flash"
            time.sleep(1.5)  # past both delays
        finally:
            models.close()

    assert argon.requests == []


# Batch acceptance, targeted check: the loser ends first and the winner's
# record then cannot be written: the request fails with that error, as it
# does without hedging (the coordinator returns only after the winner's end).
def test_a_winners_record_failing_after_the_loser_ended_fails_the_request(tmp_path):
    with FakeModel() as flash, FakeModel() as argon:
        flash.inject_error("timeout")  # the loser; cancelled as the copy wins
        models, log = _models(flash, argon, tmp_path)
        append = log.append

        def failing_winner_record(**fields) -> None:
            if fields["result"] == "succeeded":
                time.sleep(1.0)  # the cancelled loser's end comes first
                raise OSError("disk full")
            append(**fields)

        log.append = failing_winner_record
        try:
            with pytest.raises(OSError, match="disk full"):
                models.flash.complete(MESSAGES, kind="translation_batch")
        finally:
            models.close()

    assert _summary(load_records(log.path)) == [(1, "flash", "cancelled_for_winner", False)]


# A loser whose record cannot be written after the request has returned
# leaves a warning; the caller does not wait for it.
def test_a_losers_record_failing_after_the_request_returned_is_logged(tmp_path, caplog):
    with FakeModel() as flash, FakeModel() as argon:
        flash.inject_error("timeout")
        models, log = _models(flash, argon, tmp_path)
        append = log.append

        def failing_loser_record(**fields) -> None:
            if fields["result"] == "cancelled_for_winner":
                time.sleep(0.5)  # after the request has returned
                raise OSError("disk full")
            append(**fields)

        log.append = failing_loser_record
        try:
            with caplog.at_level("WARNING", logger="zotero_translate.model_client"):
                started = time.monotonic()
                assert models.flash.complete(MESSAGES, kind="translation_batch") == "ok"
                assert time.monotonic() - started < HEDGE + 0.4  # did not wait for the loser
                assert _wait_until(lambda: "could not be written" in caplog.text, timeout=3.0)
        finally:
            models.close()
    assert "disk full" in caplog.text


# M4 ticket 17: an answer that passes the check can echo the key too. It is
# redacted as it arrives, with the keys of both endpoints (the client that
# started the request knows both), before the check, the cache and the caller.
def _assert_keyless(tmp_path: Path, models, log: RequestLog, expected: str) -> None:
    key = ResponseCache.key(models.flash._body(MESSAGES))
    entry = ResponseCache(tmp_path / "cache").entry(key)
    assert entry is not None and entry["content"] == expected, entry
    for path in tmp_path.rglob("*"):
        if path.is_file():
            for secret in (FAKE_API_KEY, FAKE_ARGON_API_KEY):
                assert secret.encode() not in path.read_bytes(), path


def test_a_hedged_argon_copy_that_wins_with_the_keys_echoed_is_returned_and_cached_without_them(tmp_path):
    echoed = f"argon {FAKE_ARGON_API_KEY} and flash {FAKE_API_KEY}"
    expected = f"argon {REDACTED} and flash {REDACTED}"
    checked = []
    with FakeModel() as flash, FakeModel() as argon:
        flash.inject_error("timeout")  # the original never answers: the copy wins
        argon.respond("other", lambda body: echoed)
        models, log = _models(flash, argon, tmp_path)
        try:
            content = models.flash.complete(MESSAGES, validate=checked.append, kind="review")
            again = models.flash.complete(MESSAGES, kind="review")  # the cache
            records = _records(log, 3)
        finally:
            models.close()

    assert content == expected and again == expected
    assert checked == [expected]
    assert _summary(records)[:2] == [(1, "flash", "cancelled_for_winner", False), (2, "argon", "succeeded", True)]
    _assert_keyless(tmp_path, models, log, expected)


def test_a_hedged_flash_send_that_wins_with_its_key_echoed_is_returned_and_cached_without_it(tmp_path):
    echoed = f"flash {FAKE_API_KEY}"
    expected = f"flash {REDACTED}"
    checked = []
    with FakeModel() as flash, FakeModel() as argon:
        flash.respond("other", _later(HEDGE * 1.5, echoed))  # past the threshold, so a copy is sent
        argon.respond("other", _later(HEDGE * 6, f"argon {FAKE_ARGON_API_KEY}"))
        models, log = _models(flash, argon, tmp_path)
        try:
            content = models.flash.complete(MESSAGES, validate=checked.append, kind="review")
            records = _records(log, 2)
        finally:
            models.close()

    assert content == expected
    assert checked == [expected]
    assert _summary(records) == [(1, "flash", "succeeded", False), (2, "argon", "cancelled_for_winner", True)]
    _assert_keyless(tmp_path, models, log, expected)


def test_a_request_forwarded_to_argon_with_the_keys_echoed_is_returned_and_cached_without_them(tmp_path):
    echoed = f"argon {FAKE_ARGON_API_KEY} and flash {FAKE_API_KEY}"
    expected = f"argon {REDACTED} and flash {REDACTED}"
    with FakeModel() as flash, FakeModel() as argon:
        flash.fail_same_request(["timeout", "timeout"])
        argon.respond("other", lambda body: echoed)
        models, log = _models(
            flash, argon, tmp_path, hedge_after=None, flash_changes={"timeout_seconds": 1.0}
        )
        try:
            content = models.flash.complete(MESSAGES, kind="translation_batch")
        finally:
            models.close()

    assert content == expected
    assert [(r["send"], r["endpoint"], r["result"]) for r in load_records(log.path)] == [
        (1, "flash", "timeout"),
        (2, "flash", "timeout"),
        (3, "argon", "succeeded"),
    ]
    _assert_keyless(tmp_path, models, log, expected)
