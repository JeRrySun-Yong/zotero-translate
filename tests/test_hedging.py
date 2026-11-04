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
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import FAKE_API_KEY
from tests.conftest import FAKE_ARGON_API_KEY
from tests.fake_model import FAKE_DOSSIER
from tests.fake_model import FakeModel
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
