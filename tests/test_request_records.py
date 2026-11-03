"""Request latency records (M3 ticket 12; spec sections 5 and 7, seam 2).

Every model request send gets one record in the attempt's task report, with
the attempt number, the request, its kind, stage and page, which send of the
request it is, the endpoint and model, the wait for a concurrency slot, the
send's own time and its result; an answer from the cache gets a record with
time 0. The records are appended to ``requests.jsonl`` as they happen, so an
interrupted attempt keeps the ones it finished.

The client tests drive the pipeline model client against the fake model; the
service tests look only at the HTTP interface, the report files and the
fake model's requests.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import EXCERPT
from tests.conftest import FAKE_API_KEY
from tests.conftest import FAKE_ARGON_API_KEY
from tests.conftest import FAKE_MODEL_NAME
from tests.conftest import FIXTURES
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.conftest import write_env_file
from tests.fake_model import FakeModel
from tests.fake_model import pseudo_translate_algorithm
from zotero_translate.model_client import ModelCallError
from zotero_translate.model_client import ModelClient
from zotero_translate.model_client import PipelineModels
from zotero_translate.model_client import ResponseCache
from zotero_translate.request_log import KINDS
from zotero_translate.request_log import REQUESTS_FILE
from zotero_translate.request_log import RESULTS
from zotero_translate.request_log import RequestLog
from zotero_translate.request_log import load_records
from zotero_translate.settings import ModelSettings
from zotero_translate.settings import PipelineSettings

FIELDS = {
    "attempt",
    "request",
    "kind",
    "stage",
    "page",
    "send",
    "endpoint",
    "model",
    "started_at",
    "slot_wait_seconds",
    "seconds",
    "result",
    "http_status",
}
# The fake model's names for the request kinds that differ from the records'.
FAKE_KINDS = {"translate_batch": "translation_batch", "algorithm": "algorithm_translation"}
MESSAGES = [{"role": "user", "content": "hello"}]
LATE_SECONDS = 4.0  # how long the late algorithm answer takes


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


def _client(model: FakeModel, tmp_path: Path, **changes) -> tuple[ModelClient, RequestLog]:
    log = RequestLog(tmp_path / "attempt" / REQUESTS_FILE, attempt=3)
    log.stage = "翻译与审校"
    return ModelClient(_settings(model, tmp_path, **changes), role="flash", request_log=log), log


def _summary(records: list[dict]) -> list[tuple]:
    return [(r["request"], r["send"], r["endpoint"], r["result"], r["http_status"]) for r in records]


def test_each_send_of_a_request_gets_a_record_with_its_result(tmp_path):
    with FakeModel() as model:
        model.fail_same_request(["500", "429", "empty"])
        client, log = _client(model, tmp_path)
        try:
            client.complete(MESSAGES, page=7, kind="translation_batch")
        finally:
            client.close()

    records = load_records(log.path)
    assert records == log.records
    assert len(records) == len(model.requests) == 4
    assert _summary(records) == [
        (1, 1, "flash", "http_error", 500),
        (1, 2, "flash", "http_error", 429),
        (1, 3, "flash", "invalid_answer", 200),
        (1, 4, "flash", "succeeded", 200),
    ]
    for record in records:
        assert set(record) == FIELDS
        assert record["attempt"] == 3
        assert (record["kind"], record["stage"], record["page"]) == (
            "translation_batch",
            "翻译与审校",
            7,
        )
        assert record["model"] == "fake-flash"
        assert record["seconds"] > 0
        assert record["started_at"] > 0
    assert FAKE_API_KEY not in log.path.read_text("utf-8")


def test_an_answer_the_caller_rejects_and_a_permanent_http_error_are_recorded(tmp_path):
    with FakeModel() as model:
        model.inject_error("401", kind="other", nth=3)
        client, log = _client(model, tmp_path)
        seen = []

        def validate(content: str) -> None:
            seen.append(content)
            if len(seen) == 1:
                log.stage = "排版"  # the paper task moves on while the request runs
                raise ValueError("first answer rejected")

        try:
            client.complete(MESSAGES, validate=validate, kind="review")
            try:
                client.complete([{"role": "user", "content": "other"}], kind="review")
            except ModelCallError as e:
                assert e.category == "permanent"
            else:
                raise AssertionError("HTTP 401 did not fail the request")
        finally:
            client.close()

    records = load_records(log.path)
    assert _summary(records) == [
        (1, 1, "flash", "invalid_answer", 200),
        (1, 2, "flash", "succeeded", 200),
        (2, 1, "flash", "http_error", 401),
    ]
    # The stage is the one the request started in, for each of its sends.
    assert [r["stage"] for r in records] == ["翻译与审校", "翻译与审校", "排版"]


def test_a_send_ended_by_any_other_error_is_recorded_and_the_error_propagates(tmp_path):
    error = RuntimeError("client closed")
    with FakeModel() as model:
        client, log = _client(model, tmp_path)

        def broken_post(body: dict, timing):
            time.sleep(0.05)
            raise error

        client._post = broken_post
        try:
            with pytest.raises(RuntimeError) as raised:
                client.complete(MESSAGES, page=5, kind="review")
        finally:
            client.close()

    assert raised.value is error
    (record,) = load_records(log.path)
    assert (record["request"], record["send"], record["result"], record["http_status"]) == (
        1,
        1,
        "error",
        None,
    )
    assert record["seconds"] >= 0.05 and record["started_at"] > 0
    assert client._in_flight == 0  # the send's slot and count were given back


def test_a_cache_hit_gets_a_record_with_time_zero(tmp_path):
    with FakeModel() as model:
        client, log = _client(model, tmp_path)
        try:
            client.complete(MESSAGES, page=2, kind="review")
            client.complete(MESSAGES, page=2, kind="review")
        finally:
            client.close()

    assert len(model.requests) == 1
    sent, hit = load_records(log.path)
    assert (sent["result"], sent["send"]) == ("succeeded", 1)
    assert hit["request"] == 2
    assert (hit["result"], hit["send"], hit["seconds"], hit["slot_wait_seconds"]) == (
        "cache_hit",
        None,
        0,
        0,
    )
    assert (hit["kind"], hit["page"], hit["endpoint"], hit["attempt"]) == ("review", 2, "flash", 3)


def test_the_wait_for_a_concurrency_slot_is_recorded_apart_from_the_send(tmp_path):
    delay = 0.6
    with FakeModel(delay=delay) as model:
        client, log = _client(model, tmp_path, concurrency=1, cache_dir=None)
        threads = [
            threading.Thread(
                target=client.complete,
                args=([{"role": "user", "content": f"request {n}"}],),
                kwargs={"kind": "translation_batch"},
            )
            for n in range(2)
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        finally:
            client.close()

    first, second = sorted(load_records(log.path), key=lambda r: r["slot_wait_seconds"])
    assert first["slot_wait_seconds"] < delay / 2
    assert second["slot_wait_seconds"] >= delay * 0.8  # it waited for the first send to end
    for record in (first, second):
        assert record["result"] == "succeeded"
        # The send's own time does not include the wait for the slot.
        assert delay * 0.8 <= record["seconds"] < delay * 1.8
    assert second["started_at"] >= first["started_at"] + delay * 0.8


def test_timeouts_and_the_forward_to_argon_are_recorded_by_endpoint(tmp_path):
    with FakeModel() as model:
        model.fail_same_request(["timeout", "timeout"])
        log = RequestLog(tmp_path / REQUESTS_FILE, attempt=1)
        models = PipelineModels(
            PipelineSettings(
                flash=_settings(model, tmp_path, timeout_seconds=0.5),
                argon=_settings(model, tmp_path, model="fake-argon", api_key=FAKE_ARGON_API_KEY),
            ),
            log,
        )
        try:
            models.flash.complete(MESSAGES, page=4, kind="translation_batch")
            # The same request again: argon's answer, cached under flash's request.
            models.flash.complete(MESSAGES, page=4, kind="translation_batch")
        finally:
            models.close()

    records = load_records(log.path)
    assert _summary(records) == [
        (1, 1, "flash", "timeout", None),
        (1, 2, "flash", "timeout", None),
        (1, 3, "argon", "succeeded", 200),
        (2, None, "argon", "cache_hit", None),
    ]
    assert [r["model"] for r in records] == ["fake-flash", "fake-flash", "fake-argon", "fake-argon"]
    assert all(0.5 <= r["seconds"] < 2 for r in records[:2])
    text = log.path.read_text("utf-8")
    assert FAKE_API_KEY not in text and FAKE_ARGON_API_KEY not in text


def test_a_cache_entry_from_before_the_records_is_recorded_as_this_clients(tmp_path):
    with FakeModel() as model:
        client, log = _client(model, tmp_path)
        cache = ResponseCache(tmp_path / "cache")
        key = ResponseCache.key(client._body(MESSAGES))
        path = cache.directory / key[:2] / f"{key}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"content": "old answer"}), encoding="utf-8")
        try:
            assert client.complete(MESSAGES, kind="review") == "old answer"
        finally:
            client.close()

    (hit,) = load_records(log.path)
    assert (hit["result"], hit["endpoint"], hit["model"]) == ("cache_hit", "flash", "fake-flash")
    assert model.requests == []


def test_a_torn_last_line_is_skipped(tmp_path):
    path = tmp_path / REQUESTS_FILE
    path.write_text('{"request": 1}\n{"request": 2, "kin', encoding="utf-8")
    assert load_records(path) == [{"request": 1}]
    assert load_records(tmp_path / "missing.jsonl") == []


def test_run_writes_the_records_next_to_its_report(tmp_path):
    with FakeModel() as model:
        model.inject_error("401", kind="term_list")  # a short run: dossier, then a failure
        output_dir = tmp_path / "out"
        command = Path(sys.executable).with_name("zotero-translate")
        subprocess.run(
            [str(command), "run", str(EXCERPT), "--output-dir", str(output_dir)]
            + ["--env-file", str(write_env_file(tmp_path, model.base_url, FAKE_API_KEY, FAKE_MODEL_NAME))]
            + ["--argon-env-file", str(write_env_file(tmp_path, model.base_url, FAKE_ARGON_API_KEY))]
            + ["--data-dir", str(tmp_path / "data")],
            capture_output=True,
            text=True,
            timeout=120,
        )

    report = json.loads((output_dir / "report.json").read_text("utf-8"))
    assert report["state"] == "failed"
    records = load_records(output_dir / REQUESTS_FILE)
    assert report["requests"] == records
    assert [(r["kind"], r["result"], r["http_status"]) for r in records] == [
        ("dossier", "succeeded", 200),
        ("term_list", "http_error", 401),
    ]
    assert {r["attempt"] for r in records} == {None}
    assert {r["endpoint"] for r in records} == {"argon"}


def _late_algorithm(body: dict) -> str:
    time.sleep(LATE_SECONDS)
    return pseudo_translate_algorithm(body)


def test_a_run_that_fails_with_an_algorithm_send_in_flight_reports_that_send(tmp_path):
    # As in the service test below: the algorithm send outlives the failure
    # report; ``run`` must still bring report.json up to date as it exits.
    with FakeModel() as model:
        model.respond("algorithm", _late_algorithm)
        model.inject_error("401", kind="translate_batch")
        output_dir = tmp_path / "out"
        command = Path(sys.executable).with_name("zotero-translate")
        timeout = LATE_SECONDS * 2  # see the service test below
        flash = write_env_file(tmp_path, model.base_url, FAKE_API_KEY, FAKE_MODEL_NAME, timeout)
        argon = write_env_file(tmp_path, model.base_url, FAKE_ARGON_API_KEY, None, timeout)
        result = subprocess.run(
            [str(command), "run", str(FIXTURES / "maliang-p6.pdf"), "--output-dir", str(output_dir)]
            + ["--env-file", str(flash), "--argon-env-file", str(argon)]
            + ["--data-dir", str(tmp_path / "data")],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert model.requests_of("algorithm")

    assert result.returncode == 1, result.stderr[-2000:]
    report = json.loads((output_dir / "report.json").read_text("utf-8"))
    assert report["state"] == "failed"
    records = load_records(output_dir / REQUESTS_FILE)
    failed = [r for r in records if r["http_status"] == 401]
    late = [r for r in records if r["kind"] == "algorithm_translation" and r["send"] == 1]
    assert len(failed) == 1 and len(late) == 1, records
    assert late[0]["started_at"] < failed[0]["started_at"]
    assert report["requests"] == records


def _attempt_dir(service, task_id: str, attempt: int) -> Path:
    return service.data_dir / "tasks" / task_id / "attempts" / str(attempt)


def test_the_task_report_holds_a_record_for_every_send_and_every_cache_hit(tmp_path):
    with FakeModel() as model:
        model.fail_same_request(["500"], kind="translate_batch")
        service = launch_service(model, tmp_path)
        try:
            first_id = service.submit()
            assert service.wait(first_id)["state"] == "succeeded"
            sent = len(model.requests)
            second_id = service.submit()  # the same paper: every answer is in the cache
            assert service.wait(second_id)["state"] == "succeeded"
            assert len(model.requests) == sent

            report = service.report(first_id)
            records = report["requests"]
            assert records == load_records(_attempt_dir(service, first_id, 1) / REQUESTS_FILE)
            # A paper can repeat a request (e.g. the same paragraph twice):
            # the repeat is a cache hit, not a send.
            sends = [r for r in records if r["result"] != "cache_hit"]
            assert len(sends) == sent
            assert {r["attempt"] for r in records} == {1}
            assert all(set(r) == FIELDS for r in records)
            assert {r["kind"] for r in records} <= set(KINDS)
            assert {r["result"] for r in records} <= set(RESULTS)
            # One record per send, by kind, matching what the endpoint received.
            by_kind: dict[str, int] = {}
            for request in model.requests:
                kind = FAKE_KINDS.get(request.kind, request.kind)
                by_kind[kind] = by_kind.get(kind, 0) + 1
            recorded: dict[str, int] = {}
            for record in sends:
                recorded[record["kind"]] = recorded.get(record["kind"], 0) + 1
            assert recorded == by_kind
            assert {"dossier", "term_list", "translation_batch", "review"} <= set(recorded)
            assert {r["endpoint"] for r in records if r["kind"] in ("dossier", "term_list")} == {
                "argon"
            }
            assert {r["stage"] for r in records if r["kind"] == "dossier"} == {"生成论文档案"}
            assert {r["stage"] for r in records if r["kind"] == "review"} == {"翻译与审校"}
            failed = [r for r in records if r["result"] == "http_error"]
            assert [(r["kind"], r["send"], r["http_status"]) for r in failed] == [
                ("translation_batch", 1, 500)
            ]
            assert any(
                r["request"] == failed[0]["request"] and r["send"] == 2 and r["result"] == "succeeded"
                for r in records
            )
            assert all(r["page"] for r in records if r["kind"] in ("translation_batch", "review"))

            hits = service.report(second_id)["requests"]
            assert len(hits) == len({r["request"] for r in records})  # one per logical request
            assert {(r["result"], r["seconds"], r["send"]) for r in hits} == {("cache_hit", 0, None)}

            for task_id in (first_id, second_id):
                directory = service.data_dir / "tasks" / task_id
                for path in (directory / "report.json", _attempt_dir(service, task_id, 1) / REQUESTS_FILE):
                    text = path.read_text("utf-8")
                    assert FAKE_API_KEY not in text and FAKE_ARGON_API_KEY not in text
        finally:
            stop_service(service)


def _quiet(model: FakeModel, seconds: float = 2.0, timeout: float = 120) -> None:
    """Wait until the fake model has received no request for ``seconds``."""
    deadline = time.monotonic() + timeout
    while True:
        count = len(model.requests)
        time.sleep(seconds)
        if len(model.requests) == count:
            return
        assert time.monotonic() < deadline, "the task never stopped sending requests"


def test_an_interrupted_attempt_keeps_the_records_from_before_the_interruption(tmp_path):
    with FakeModel() as model:
        model.inject_error("timeout", kind="review")  # attempt 1 hangs on its first review
        service = launch_service(model, tmp_path)
        restarted = None
        try:
            task_id = service.submit()
            service.wait_for(task_id, lambda s: bool(model.requests_of("review")))
            _quiet(model)
            answered = [r for r in model.requests if r.fault is None]

            service.process.kill()  # as a crash would; the worker is cut short too
            service.process.wait(timeout=10)
            restarted = launch_service(model, tmp_path, data_dir=service.data_dir)
            assert restarted.wait(task_id)["state"] == "succeeded"

            first = json.loads((_attempt_dir(restarted, task_id, 1) / "report.json").read_text("utf-8"))
            assert first["complete"] is False
            records = first["requests"]
            # Every send answered before the interruption, none for the hung one.
            sends = [r for r in records if r["result"] != "cache_hit"]
            assert len(sends) == len(answered)
            assert {r["result"] for r in sends} == {"succeeded"}
            assert {r["attempt"] for r in records} == {1}
            assert {"dossier", "term_list", "translation_batch"} <= {r["kind"] for r in records}

            second = restarted.report(task_id)
            assert second["attempt"] == 2
            assert {r["attempt"] for r in second["requests"]} == {2}
            assert "cache_hit" in {r["result"] for r in second["requests"]}
        finally:
            if restarted is not None:
                stop_service(restarted)
            stop_service(service)


def test_a_send_that_ends_after_the_task_failed_is_in_the_attempts_records(tmp_path):
    # The algorithm request starts before the batches and answers late; a
    # batch fails the task with HTTP 401 meanwhile. The worker writes its
    # failure report while the algorithm send is still in flight, and that
    # send ends afterwards. (Closing the client then leaves that send waiting
    # until the request timeout, which is therefore small here; see backlog.)
    with FakeModel() as model:
        model.respond("algorithm", _late_algorithm)
        model.inject_error("401", kind="translate_batch")
        service = launch_service(model, tmp_path, request_timeout=LATE_SECONDS * 2)
        try:
            task_id = service.submit(FIXTURES / "maliang-p6.pdf")
            final = service.wait(task_id)
            assert final["state"] == "failed"
            assert "HTTP 401" in final["error"]
            assert model.requests_of("algorithm")

            directory = _attempt_dir(service, task_id, 1)
            records = load_records(directory / REQUESTS_FILE)
            failed = [r for r in records if r["http_status"] == 401]
            late = [r for r in records if r["kind"] == "algorithm_translation" and r["send"] == 1]
            assert len(failed) == 1 and len(late) == 1, records
            # The algorithm send started before the failure and ended after it.
            assert late[0]["started_at"] < failed[0]["started_at"]
            assert late[0]["seconds"] > 1
            report = json.loads((directory / "report.json").read_text("utf-8"))
            assert report["state"] == "failed"
            assert report["requests"] == records
            assert service.report(task_id)["requests"] == records
        finally:
            stop_service(service)
