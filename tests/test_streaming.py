"""Streamed requests for user profiles (M4 ticket 12).

Command Code's DeepSeek Fast answers a plain request that thinks longer
than about 125 seconds with HTTP 524; the same request with ``"stream":
true`` succeeds. A model setting turns streaming on (``ZT_STREAM=1`` in an
endpoint file, ``stream`` in a profile): the request body then has
``"stream": true`` and the client joins the ``delta.content`` of the event
stream into the answer, which then goes through the caller's check as
before. With streaming off, the body is exactly what it was before (the
built-in Gemini's cache keys do not change).

These tests drive the pipeline model client against the fake model; the
profile setting through the service is in ``test_model_profiles.py``.
"""

import json
import logging
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import FAKE_API_KEY
from tests.conftest import FAKE_ARGON_API_KEY
from tests.fake_model import STREAM_THINKING
from tests.fake_model import FakeModel
from zotero_translate.endpoint import SendCancelled
from zotero_translate.model_client import ModelCallError
from zotero_translate.model_client import ModelClient
from zotero_translate.model_client import PipelineModels
from zotero_translate.model_client import REDACTED
from zotero_translate.model_client import ResponseCache
from zotero_translate.request_log import REQUESTS_FILE
from zotero_translate.request_log import RequestLog
from zotero_translate.request_log import load_records
from zotero_translate.settings import ModelSettings
from zotero_translate.settings import PipelineSettings
from zotero_translate.settings import SettingsError
from zotero_translate.settings import load_model_settings
from zotero_translate.settings import load_pipeline_settings
from zotero_translate.settings import load_profile_settings

MESSAGES = [{"role": "user", "content": "hello"}]
ANSWER = '{"terms": ["meta-harness", "代码智能体"], "note": "一段足够长、要分成好几块发送的回答。"}'


def _settings(fake: FakeModel, tmp_path: Path, **changes) -> ModelSettings:
    values = dict(
        base_url=fake.base_url,
        api_key=FAKE_API_KEY,
        model="fake-deepseek",
        concurrency=4,
        timeout_seconds=5.0,
        retry_backoff_seconds=0.01,
        connection_retry_seconds=(0.01, 0.01, 0.01),
        cache_dir=tmp_path / "cache",
    )
    values.update(changes)
    return ModelSettings(**values)


def test_a_streamed_answer_is_the_content_deltas_joined_without_the_thinking(tmp_path):
    with FakeModel() as fake:
        fake.respond("other", lambda body: ANSWER)
        client = ModelClient(_settings(fake, tmp_path, stream=True))
        try:
            content = client.complete(MESSAGES, validate=json.loads)
        finally:
            client.close()

    assert content == ANSWER
    assert all(part not in content for part in STREAM_THINKING)
    [request] = fake.requests
    assert request.body["stream"] is True
    assert request.fault is None and not request.disconnected


def test_a_stream_that_breaks_off_before_its_finish_reason_is_resent_and_recorded(tmp_path):
    log = RequestLog(tmp_path / REQUESTS_FILE, attempt=1)
    log.stage = "翻译与审校"
    with FakeModel() as fake:
        fake.respond("other", lambda body: ANSWER)
        fake.fail_same_request(["stream_cut"])
        client = ModelClient(_settings(fake, tmp_path, stream=True), role="flash", request_log=log)
        try:
            content = client.complete(MESSAGES, validate=json.loads, page=2, kind="term_list")
        finally:
            client.close()

    assert content == ANSWER
    assert [r.fault for r in fake.requests] == ["stream_cut", None]
    assert fake.requests[0].body == fake.requests[1].body
    records = load_records(log.path)
    assert [(r["send"], r["result"], r["http_status"]) for r in records] == [
        (1, "invalid_answer", 200),
        (2, "succeeded", 200),
    ]
    assert all(r["kind"] == "term_list" and r["endpoint"] == "flash" for r in records)
    assert all(r["seconds"] > 0 and r["started_at"] for r in records)
    assert client.retry_count == 1 and client.request_count == 2


def test_a_stream_that_breaks_off_every_time_fails_with_a_readable_reason(tmp_path):
    with FakeModel() as fake:
        fake.fail_same_request(["stream_cut"] * 4)
        client = ModelClient(_settings(fake, tmp_path, stream=True))
        try:
            with pytest.raises(ModelCallError) as failure:
                client.complete(MESSAGES)
        finally:
            client.close()

    assert "流式回答在结束之前中断" in str(failure.value)
    assert failure.value.category == "retryable"
    assert len(fake.requests) == 4


# The cache key of this body as the client built it before ticket 12
# (computed at 6ed6590): with streaming off, Gemini's cached answers still hit.
GEMINI_KEY_BEFORE = "d0ab888fdff3b1952e09de2a3b728ab03bb2643bfd0ec919115cbf27430ba02d"


def test_with_streaming_off_the_request_body_and_its_cache_key_are_as_before(tmp_path):
    with FakeModel() as fake:
        settings = _settings(fake, tmp_path, model="gemini-3.8-flash-high")
        assert settings.stream is False  # the default
        client = ModelClient(settings)
        try:
            assert client.complete(MESSAGES) == "ok"
            body = client._body(MESSAGES)
        finally:
            client.close()

    [request] = fake.requests
    before = {
        "model": "gemini-3.8-flash-high",
        "messages": MESSAGES,
        "temperature": 1.0,
        "reasoning_effort": "high",
    }
    # The bytes on the wire: httpx's JSON of the body as it was, key order included.
    assert request.raw_body == json.dumps(before, ensure_ascii=False, separators=(",", ":")).encode()
    assert ResponseCache.key(body) == GEMINI_KEY_BEFORE
    assert ResponseCache.key({**body, "stream": True}) != GEMINI_KEY_BEFORE


def _env_file(tmp_path: Path, name: str, extra: str = "") -> Path:
    path = tmp_path / name
    path.write_text(f"OPENAI_BASE_URL=http://127.0.0.1:9/v1\nOPENAI_API_KEY=sk-x\nAI_DEFAULT_MODEL=m\n{extra}")
    return path


@pytest.mark.parametrize(
    "line, stream",
    [("", False), ("ZT_STREAM=1\n", True), ("ZT_STREAM=0\n", False), ('ZT_STREAM="1"\n', True)],
    ids=["absent", "on", "off", "quoted"],
)
def test_an_endpoint_file_turns_streaming_on_with_zt_stream(tmp_path, line, stream):
    path = _env_file(tmp_path, "endpoint.env", line)
    pipeline = load_pipeline_settings(path, path)
    assert pipeline.flash.stream is stream and pipeline.argon.stream is stream
    profile = load_profile_settings(path)
    assert profile.flash.stream is stream and profile.argon.stream is stream


def test_an_endpoint_file_with_another_zt_stream_value_is_refused(tmp_path):
    path = _env_file(tmp_path, "endpoint.env", "ZT_STREAM=yes please\n")
    with pytest.raises(SettingsError, match="ZT_STREAM"):
        load_model_settings(path)


def _wait_until(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


# A stream whose bytes keep coming never meets httpx's read timeout: the
# request timeout cuts it, as it cuts a plain response that trickles in.
def test_a_stream_still_running_at_the_request_timeout_is_cut_off_and_resent(tmp_path):
    log = RequestLog(tmp_path / REQUESTS_FILE, attempt=1)
    log.stage = "翻译与审校"
    with FakeModel() as fake:
        fake.fail_same_request(["stream_trickle"])
        settings = _settings(fake, tmp_path, stream=True, timeout_seconds=1.0)
        client = ModelClient(settings, role="flash", request_log=log)
        try:
            started = time.monotonic()
            assert client.complete(MESSAGES, kind="review") == "ok"
            took = time.monotonic() - started
            assert _wait_until(lambda: fake.requests[0].disconnected)
        finally:
            client.close()

    assert 1.0 <= took < 4.0
    records = load_records(log.path)
    assert [r["result"] for r in records] == ["timeout", "succeeded"]
    assert client.timeout_count == 1


def test_closing_the_client_aborts_a_stream_in_flight(tmp_path):
    with FakeModel() as fake:
        fake.inject_error("stream_trickle")
        client = ModelClient(_settings(fake, tmp_path, stream=True, timeout_seconds=60.0))
        failure: list[BaseException] = []

        def call() -> None:
            try:
                client.complete(MESSAGES)
            except BaseException as e:  # noqa: BLE001 - the test reads it
                failure.append(e)

        caller = threading.Thread(target=call)
        caller.start()
        assert _wait_until(lambda: len(fake.requests) == 1)
        time.sleep(0.3)  # thinking deltas are coming in
        closed = time.monotonic()
        client.close()
        caller.join(10)
        assert not caller.is_alive()
        assert time.monotonic() - closed < 2.0
        assert _wait_until(lambda: fake.requests[0].disconnected)

    assert [type(e) for e in failure] == [SendCancelled]
    assert client.endpoint.in_flight == 0


def test_a_hedged_copy_that_wins_aborts_the_streaming_original(tmp_path):
    hedge = 0.5
    log = RequestLog(tmp_path / REQUESTS_FILE, attempt=1)
    log.stage = "翻译与审校"
    with FakeModel() as flash, FakeModel() as argon:
        flash.inject_error("stream_trickle")
        settings = PipelineSettings(
            flash=_settings(flash, tmp_path, stream=True, model="fake-flash"),
            argon=_settings(argon, tmp_path, stream=True, model="fake-argon", api_key=FAKE_ARGON_API_KEY),
            hedge_after_seconds=hedge,
        )
        models = PipelineModels(settings, log)
        try:
            assert models.flash.complete(MESSAGES, page=1, kind="translation_batch") == "ok"
            assert _wait_until(lambda: flash.requests[0].disconnected)
            assert _wait_until(lambda: len(load_records(log.path)) == 2)
        finally:
            models.close()

    records = sorted(load_records(log.path), key=lambda r: r["send"])
    assert [(r["send"], r["endpoint"], r["result"], r["hedge"]) for r in records] == [
        (1, "flash", "cancelled_for_winner", False),
        (2, "argon", "succeeded", True),
    ]
    assert argon.requests[0].body["stream"] is True


# Malformed stream events are "unparseable", as a malformed plain answer is:
# they are retried (no cache, no crash) and the final reason says so.
MALFORMED_STREAM_EVENTS = [
    ('{"choices": 1}', "choices-not-a-list"),
    ('{"choices": "x"}', "choices-a-string"),
    ('{"choices": [1]}', "choice-not-an-object"),
    ('{"choices": ["x"]}', "choice-a-string"),
    ('{"choices": [{"index": 0, "delta": "x"}]}', "delta-not-an-object"),
    ('{"choices": [{"index": 0, "delta": {"content": ["ok"]}}]}', "content-not-text"),
    ('{"choices": [{"index": 0, "delta": {"content": 1}}]}', "content-a-number"),
]


@pytest.mark.parametrize(
    "event", [event for event, _ in MALFORMED_STREAM_EVENTS], ids=[name for _, name in MALFORMED_STREAM_EVENTS]
)
def test_a_malformed_stream_event_is_retried_like_an_unparseable_answer(tmp_path, event):
    with FakeModel() as fake:
        fake.stream_body = f"data: {event}\n\ndata: [DONE]\n\n".encode()
        client = ModelClient(_settings(fake, tmp_path, stream=True))
        try:
            with pytest.raises(ModelCallError) as failure:
                client.complete(MESSAGES)
        finally:
            client.close()

    assert "无法解析管线模型的流式响应" in str(failure.value)
    assert failure.value.category == "retryable"
    assert len(fake.requests) == 4  # 1 + max_retries
    assert not (tmp_path / "cache").exists()  # a failed answer is never cached
    assert FAKE_API_KEY not in str(failure.value)


@pytest.mark.parametrize("event_field", ["", "event: error\n"], ids=["data-only", "event-error"])
def test_an_error_event_in_a_stream_is_retryable_and_redacts_the_key(tmp_path, event_field):
    error = json.dumps({"error": {"message": f"the endpoint rejected the key {FAKE_API_KEY}"}})
    with FakeModel() as fake:
        fake.stream_body = f"{event_field}data: {error}\n\ndata: [DONE]\n\n".encode()
        client = ModelClient(_settings(fake, tmp_path, stream=True))
        try:
            with pytest.raises(ModelCallError) as failure:
                client.complete(MESSAGES)
        finally:
            client.close()

    message = str(failure.value)
    assert "管线模型在流式回答中出错" in message
    assert REDACTED in message
    assert FAKE_API_KEY not in message
    assert failure.value.category == "retryable"
    assert not (tmp_path / "cache").exists()


# An "event: error" is a failure whatever its data holds, also when the answer
# was finished already: the endpoint said the stream failed.
ERROR_EVENTS = [
    ('event: error\ndata: {"message": "failed"}', "json-without-error-field"),
    ("event: error\ndata: this is not json", "data-not-json"),
    ("event: error", "no-data"),
]


@pytest.mark.parametrize("event", [e for e, _ in ERROR_EVENTS], ids=[n for _, n in ERROR_EVENTS])
def test_an_error_event_after_a_finished_answer_fails_the_request_and_is_not_cached(tmp_path, event):
    with FakeModel() as fake:
        fake.stream_body = (
            b'data: {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": null}]}\n\n'
            b'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}\n\n'
            + event.encode()
            + b'\n\ndata: [DONE]\n\n'
        )
        client = ModelClient(_settings(fake, tmp_path, stream=True))
        try:
            with pytest.raises(ModelCallError) as failure:
                client.complete(MESSAGES)
        finally:
            client.close()

    assert "管线模型在流式回答中出错" in str(failure.value)
    assert failure.value.category == "retryable"
    assert len(fake.requests) == 4  # 1 + max_retries
    assert not (tmp_path / "cache").exists()


def test_a_stream_event_whose_data_is_not_json_is_retried(tmp_path):
    with FakeModel() as fake:
        fake.stream_body = b'data: this is not json\n\ndata: [DONE]\n\n'
        client = ModelClient(_settings(fake, tmp_path, stream=True))
        try:
            with pytest.raises(ModelCallError) as failure:
                client.complete(MESSAGES)
        finally:
            client.close()

    assert "无法解析管线模型的流式响应" in str(failure.value)
    assert failure.value.category == "retryable"
    assert len(fake.requests) == 4
    assert not (tmp_path / "cache").exists()


def test_one_event_may_be_split_across_several_data_lines(tmp_path):
    with FakeModel() as fake:
        fake.stream_body = (
            b'data: {"choices": [{"index": 0,\n'
            b'data: "delta": {"content": "multi-line"}, "finish_reason": null}]}\n\n'
            b'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        client = ModelClient(_settings(fake, tmp_path, stream=True))
        try:
            assert client.complete(MESSAGES) == "multi-line"
        finally:
            client.close()

    [request] = fake.requests
    assert request.fault is None and not request.disconnected


def test_a_streamed_answer_echoing_the_key_that_fails_its_check_reads_redacted(tmp_path, caplog):
    """M4 ticket 16 item 1: the check's error holds the joined answer, key included."""
    echoed = f"meta-harness {FAKE_API_KEY} 回显"

    def reject(content: str) -> None:
        raise ValueError(f"术语条目 {content} 缺少判断依据 reason")

    with FakeModel() as fake:
        fake.respond("other", lambda body: echoed)
        client = ModelClient(_settings(fake, tmp_path, stream=True, max_retries=1))
        try:
            with caplog.at_level(logging.WARNING):
                with pytest.raises(ModelCallError) as failure:
                    client.complete(MESSAGES, validate=reject)
        finally:
            client.close()

    error = failure.value
    assert [r.get("stream") for r in (q.body for q in fake.requests)] == [True, True]
    assert FAKE_API_KEY not in str(error), str(error)
    assert f"术语条目 meta-harness {REDACTED} 回显 缺少判断依据 reason" in str(error), str(error)
    assert FAKE_API_KEY not in "".join(str(r) for r in error.rejections)
    assert len(error.rejections) == 2
    assert FAKE_API_KEY not in caplog.text and REDACTED in caplog.text, caplog.text
