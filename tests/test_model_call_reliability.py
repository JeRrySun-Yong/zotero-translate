"""Model call reliability: bounded retries, failure with a page, response cache, restarts.

Spec "计划覆盖的行为" 10, 14, 15 and 16 (13 is in test_paper_task.py), and
ticket 11: a request that runs longer than the request timeout in total.

Closure ticket 01 (orchestrator 2026-10-28 09:45, after the relay endpoint
answered one valid request with HTTP 400 in a real run): HTTP 400 is retried
like a 5xx; 401 still fails at once. An HTTP error's reason, in the task log
and in the failure reason, carries the first 200 characters of the response
body, on one line, with the API keys replaced by "[redacted]"; a 401 or 403
carries no body.

Closure ticket 09 (orchestrator 2026-10-29, after both endpoints were
unreachable for about a minute in a real run): a request that cannot reach
the endpoint waits 5, 15 and then 45 seconds before its resends, so a
network outage of about a minute costs no paper; the attempts stay 4.

M4 ticket 16 item 1: a validation error (the caller's ``validate`` rejects an
answer) can hold the answer's text; when the answer echoes the API key, the
key reads "[redacted]" in the retry log, the error and the saved rejections.

M3 ticket 05: an attempt that fails for a retryable reason no longer ends
the task; it waits for an automatic rerun (``waiting_rerun``), whose reason
these tests check. A 401 still fails the task at once.
"""

import collections
import dataclasses
import json
import logging
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from zotero_translate.model_client import REDACTED
from zotero_translate.model_client import ModelCallError
from zotero_translate.model_client import ModelClient
from zotero_translate.paper_task import DOSSIER_STAGE
from zotero_translate.review import BatchReviewer
from zotero_translate.review import KeptSource
from zotero_translate.settings import ModelSettings

from tests.conftest import EXCERPT
from tests.conftest import FAKE_API_KEY
from tests.conftest import FAKE_ARGON_API_KEY
from tests.conftest import FAKE_MODEL_NAME
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.conftest import write_env_file
from tests.fake_model import FAKE_DOSSIER
from tests.fake_model import REJECTION_BODY
from tests.fake_model import REJECTION_MESSAGE
from tests.fake_model import FakeModel

RESTART_ERROR = "翻译服务重启，任务中断"
# A small request timeout (seconds) so timeouts happen fast; production uses 480.
SMALL_TIMEOUT = 2.0
FIRST_RETRY_WAIT = 1.0  # the client's first wait before a resend


def _prompt(request) -> str:
    return json.dumps(request.body["messages"], ensure_ascii=False)


def test_batch_failing_twice_then_succeeding_lets_the_task_succeed(fake_model, start_service):
    fake_model.fail_same_request(["429", "500"], kind="translate_batch")
    service = start_service(fake_model)

    task_id = service.submit()
    final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    failing = [r for r in fake_model.requests if r.fault]
    assert [r.fault for r in failing] == ["429", "500"]
    sends = [r for r in fake_model.requests if _prompt(r) == _prompt(failing[0])]
    assert [r.fault for r in sends] == ["429", "500", None]
    report = service.report(task_id)
    assert report["model"]["retries"] == 2
    assert report["failed_batches"] == []


def test_batch_failing_four_times_fails_the_task_and_a_resubmit_reuses_the_cache(
    fake_model, start_service
):
    fake_model.fail_same_request(["500", "429", "empty", "invalid_json"], kind="translate_batch")
    service = start_service(fake_model)

    first = service.wait_first_failure(service.submit())

    assert first["state"] == "waiting_rerun"  # retryable: the service reruns it later
    assert "管线模型" in first["error"]
    assert "第 1 页" in first["error"], first["error"]  # the excerpt has one page
    assert first["files"] == ["terms"]  # made before translation; no PDF
    first_report = service.report(first["task_id"])
    assert len(first_report["failed_batches"]) >= 1
    assert first_report["failed_batches"][0]["page"] == 1
    assert first_report["model"]["retries"] >= 3

    first_run = list(fake_model.requests)
    failed_prompts = {_prompt(r) for r in first_run if r.fault}
    assert len(failed_prompts) == 1
    succeeded_prompts = {_prompt(r) for r in first_run if r.fault is None}

    second = service.wait(service.submit())

    assert second["state"] == "succeeded", second["error"]
    second_run = fake_model.requests[len(first_run):]
    resent = collections.Counter(_prompt(r) for r in second_run)
    assert not set(resent) & succeeded_prompts, "a request that succeeded before was sent again"
    assert resent[next(iter(failed_prompts))] == 1
    second_report = service.report(second["task_id"])
    assert second_report["model"]["requests"] == len(second_run)
    # Identical short paragraphs repeat within a run, so hits can exceed
    # the number of distinct prompts.
    assert second_report["model"]["cache_hits"] >= len(succeeded_prompts) > 0


def test_a_slow_response_is_cut_off_at_the_request_timeout_and_resent(
    fake_model, start_service
):
    # Headers at once, then the body slowly over 30 s: every gap between two
    # reads is short, but the whole response takes far longer than the timeout.
    fake_model.slow_body_seconds = 30.0
    fake_model.fail_same_request(["slow"], kind="translate_batch")
    service = start_service(fake_model, request_timeout=SMALL_TIMEOUT)

    final = service.wait(service.submit())

    assert final["state"] == "succeeded", final["error"]
    [slow] = [r for r in fake_model.requests if r.fault == "slow"]
    resends = [r for r in fake_model.requests if r is not slow and _prompt(r) == _prompt(slow)]
    assert [r.fault for r in resends] == [None], "the slow request was not resent"
    waited = resends[0].received_at - slow.received_at
    assert SMALL_TIMEOUT <= waited < SMALL_TIMEOUT + FIRST_RETRY_WAIT + 3, waited
    assert service.report(final["task_id"])["model"]["retries"] == 1


def test_a_request_timing_out_four_times_fails_the_task_with_the_timeout_and_page(
    fake_model, start_service
):
    # "timeout" sends nothing at all; "slow" sends headers, then trickles.
    fake_model.slow_body_seconds = 30.0
    fake_model.fail_same_request(["slow", "timeout", "slow", "slow"], kind="translate_batch")
    service = start_service(fake_model, request_timeout=SMALL_TIMEOUT)

    final = service.wait_first_failure(service.submit())

    assert final["state"] == "waiting_rerun"
    assert "管线模型请求超时（2 秒）" in final["error"], final["error"]
    assert "第 1 页" in final["error"], final["error"]
    report = service.report(final["task_id"])
    assert report["failed_batches"][0]["page"] == 1
    assert report["model"]["retries"] >= 3


def _excerpt(body: str) -> str:
    """The first 200 characters of ``body`` on one line, as the client writes them."""
    return " ".join(body.split())[:200]


def test_a_batch_rejected_once_with_http_400_is_resent_and_the_task_succeeds(
    fake_model, start_service
):
    fake_model.fail_same_request(["400"], kind="translate_batch")
    service = start_service(fake_model)

    task_id = service.submit()
    final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    [rejected] = [r for r in fake_model.requests if r.fault]
    sends = [r for r in fake_model.requests if _prompt(r) == _prompt(rejected)]
    assert [r.fault for r in sends] == ["400", None]
    assert service.report(task_id)["model"]["retries"] == 1
    log = (service.data_dir / "tasks" / task_id / "task.log").read_text("utf-8")
    [warning] = [line for line in log.splitlines() if "HTTP 400" in line]
    assert "attempt 1/4" in warning, warning
    assert _excerpt(REJECTION_BODY) in warning, warning
    assert "x" * 200 not in warning, "the response body is not cut at 200 characters"


def test_a_batch_rejected_four_times_with_http_400_fails_the_task_with_its_page_and_the_response(
    fake_model, start_service
):
    fake_model.fail_same_request(["400", "400", "400", "400"], kind="translate_batch")
    service = start_service(fake_model)

    final = service.wait_first_failure(service.submit())

    assert final["state"] == "waiting_rerun"
    assert "HTTP 400" in final["error"], final["error"]
    assert "第 1 页" in final["error"], final["error"]
    assert REJECTION_MESSAGE in final["error"], final["error"]
    assert "\n" not in final["error"]
    [rejected, *_] = [r for r in fake_model.requests if r.fault]
    sends = [r for r in fake_model.requests if _prompt(r) == _prompt(rejected)]
    assert [r.fault for r in sends] == ["400"] * 4
    assert service.report(final["task_id"])["model"]["retries"] == 3


def test_a_batch_rejected_with_http_401_fails_the_task_at_once(fake_model, start_service):
    fake_model.fail_same_request(["401"], kind="translate_batch")
    service = start_service(fake_model)

    final = service.wait(service.submit())

    assert final["state"] == "failed"
    assert final["error"].endswith("管线模型拒绝了请求（HTTP 401）"), final["error"]
    assert "injected 401" not in final["error"], final["error"]
    [rejected] = [r for r in fake_model.requests if r.fault]
    sends = [r for r in fake_model.requests if _prompt(r) == _prompt(rejected)]
    assert len(sends) == 1, "a 401 was resent"
    assert service.report(final["task_id"])["model"]["retries"] == 0


def test_an_api_key_echoed_in_a_400_response_is_redacted_everywhere(fake_model, start_service):
    fake_model.fail_same_request(["400_echo"] * 4, kind="translate_batch")
    service = start_service(fake_model)

    task_id = service.submit()
    final = service.wait_first_failure(task_id)

    assert final["state"] == "waiting_rerun"
    assert any(FAKE_API_KEY in r.headers.get("Authorization", "") for r in fake_model.requests)
    assert "Bearer [redacted]" in final["error"], final["error"]
    assert FAKE_API_KEY not in final["error"]
    for path in service.data_dir.rglob("*"):
        if path.is_file():
            assert FAKE_API_KEY.encode() not in path.read_bytes(), path.name
    for path in ("/health", f"/tasks/{task_id}"):
        assert FAKE_API_KEY.encode() not in service.http.get(path).content, path


def _unused_port() -> int:
    with socket.socket() as probe:  # a port that nothing listens on
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_unreachable_endpoint_fails_the_task_with_a_readable_reason(tmp_path, start_service):
    service = start_service(f"http://127.0.0.1:{_unused_port()}/v1")

    # The dossier request waits 5 + 15 + 45 seconds before giving up.
    final = service.wait_first_failure(service.submit(), timeout=180)

    assert final["state"] == "waiting_rerun"
    assert "无法连接管线模型" in final["error"], final["error"]


# The endpoint stays unreachable this long after the first request: past the
# old backoff (1 + 2 + 4 seconds), within the first two waits (5 + 15).
OUTAGE_SECONDS = 10.0


def test_an_endpoint_unreachable_for_seconds_is_waited_out_and_the_task_succeeds(start_service):
    port = _unused_port()
    service = start_service(f"http://127.0.0.1:{port}/v1")
    task_id = service.submit()
    service.wait_for(task_id, lambda s: s["stage"] == DOSSIER_STAGE, timeout=60)
    outage_began = time.monotonic()  # at most a poll after the dossier request's first attempt

    time.sleep(OUTAGE_SECONDS)
    with FakeModel(port=port) as model:
        final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    log = (service.data_dir / "tasks" / task_id / "task.log").read_text("utf-8")
    waits = [float(w) for w in re.findall(r"无法连接管线模型; retrying in ([\d.]+)s", log)]
    assert waits == [5.0, 15.0], log
    # The third attempt of the dossier request is the first to arrive.
    assert model.requests[0].kind == "dossier"
    assert model.requests[0].received_at - outage_began >= sum(waits) - 1
    assert service.report(task_id)["model"]["retries"] == 2


def test_run_exits_with_a_nonzero_code_when_the_paper_task_fails(tmp_path, fake_model):
    fake_model.inject_error("401", kind="dossier")
    output_dir = tmp_path / "out"
    # The installed command, as run scripts call it.
    command = Path(sys.executable).with_name("zotero-translate")

    result = subprocess.run(
        [str(command), "run", str(EXCERPT), "--output-dir", str(output_dir)]
        + ["--env-file", str(write_env_file(tmp_path, fake_model.base_url, FAKE_API_KEY, FAKE_MODEL_NAME))]
        + ["--argon-env-file", str(write_env_file(tmp_path, fake_model.base_url, FAKE_ARGON_API_KEY))]
        + ["--data-dir", str(tmp_path / "data")],
        capture_output=True,
        text=True,
        timeout=120,
    )

    report = json.loads((output_dir / "report.json").read_text("utf-8"))
    assert report["state"] == "failed"
    assert result.returncode != 0, result.stderr
    assert "论文任务失败" in result.stderr


def test_restart_requeues_unfinished_tasks_and_stops_the_old_subprocess(tmp_path):
    # M3 ticket 04: a restart no longer fails unfinished tasks; they run again.
    with FakeModel(delay=1.0) as model:
        service = launch_service(model, tmp_path)
        restarted = None
        try:
            running = service.submit()
            queued = service.submit()
            service.wait_for(running, lambda s: s["state"] == "running" and model.requests)
            task_dir = service.data_dir / "tasks" / running
            old = subprocess.run(["pgrep", "-f", str(task_dir)], capture_output=True, text=True)
            old_workers = [int(pid) for pid in old.stdout.split()]
            assert old_workers

            # Only the service: its task subprocess must end itself.
            service.process.kill()
            service.process.wait(timeout=10)
            restarted = launch_service(model, tmp_path, data_dir=service.data_dir)

            assert restarted.status(running)["state"] in {"queued", "running"}
            assert restarted.status(queued)["state"] == "queued"
            # The HTTP interface answers only after the restored service has
            # reaped the old processes; the old worker also ends itself.
            states = subprocess.run(
                ["ps", "-o", "stat=", "-p", ",".join(map(str, old_workers))],
                capture_output=True,
                text=True,
            ).stdout.split()
            assert [s for s in states if not s.startswith("Z")] == [], "the old subprocess still runs"
            for task_id, attempt in ((running, 2), (queued, 1)):
                final = restarted.wait(task_id)
                assert final["state"] == "succeeded", final["error"]
                assert final["attempt"] == attempt
            assert restarted.report(running)["attempts"][0]["reason"] == RESTART_ERROR
        finally:
            if restarted is not None:
                stop_service(restarted)
            # The old service's group, should the test end before its subprocess did.
            stop_service(service)


def test_api_key_appears_in_no_report_log_or_http_response(fake_model, start_service):
    fake_model.fail_same_request(["500"], kind="translate_batch")
    service = start_service(fake_model)

    task_id = service.submit()
    final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    task_dir = service.data_dir / "tasks" / task_id
    assert (task_dir / "report.json").is_file()
    for path in task_dir.rglob("*"):
        if path.is_file():
            assert FAKE_API_KEY.encode() not in path.read_bytes(), path.name
    for path in ("/health", f"/tasks/{task_id}", f"/tasks/{task_id}/files/mono"):
        assert FAKE_API_KEY.encode() not in service.http.get(path).content, path


# -- M4 ticket 16 item 1: a key echoed in an answer that fails validation ----


def _client(fake: FakeModel, tmp_path: Path) -> ModelClient:
    return ModelClient(
        ModelSettings(
            base_url=fake.base_url,
            api_key=FAKE_API_KEY,
            model=FAKE_MODEL_NAME,
            concurrency=2,
            timeout_seconds=5.0,
            retry_backoff_seconds=0.01,
            connection_retry_seconds=(0.01, 0.01, 0.01),
            max_retries=1,
            cache_dir=tmp_path / "cache",
        )
    )


def _reject_naming(content: str) -> None:
    """A check that puts the answer into its error, as the term list's does."""
    raise ValueError(f"术语条目 {content} 缺少判断依据 reason")


def test_a_validation_error_echoing_the_key_reads_redacted_in_the_error_the_log_and_the_rejections(
    tmp_path, caplog
):
    with FakeModel() as fake:
        fake.respond("other", lambda body: f"meta-harness {FAKE_API_KEY}")
        client = _client(fake, tmp_path)
        try:
            with caplog.at_level(logging.WARNING):
                with pytest.raises(ModelCallError) as failure:
                    client.complete([{"role": "user", "content": "hi"}], validate=_reject_naming)
        finally:
            client.close()

    error = failure.value
    assert error.answered
    assert FAKE_API_KEY not in str(error), str(error)
    assert f"术语条目 meta-harness {REDACTED} 缺少判断依据 reason" in str(error), str(error)
    assert [str(r) for r in error.rejections] == [
        f"术语条目 meta-harness {REDACTED} 缺少判断依据 reason"
    ] * 2
    assert all(isinstance(r, ValueError) for r in error.rejections)
    assert FAKE_API_KEY not in caplog.text, caplog.text
    assert REDACTED in caplog.text
    assert FAKE_API_KEY not in str(error.__cause__)


def test_a_term_list_echoing_the_key_leaves_it_out_of_the_log_the_reports_and_the_status(
    fake_model, start_service
):
    # The entry lacks its ``reason``: the check's error names the entry, and
    # the entry's English spelling is the key the model echoed.
    entry = {"english": [FAKE_ARGON_API_KEY], "category": "basic_term", "chinese": "密钥"}
    fake_model.respond("term_list", lambda body: json.dumps({"terms": [entry]}))
    service = start_service(fake_model)

    task_id = service.submit()
    final = service.wait_first_failure(task_id)

    assert final["state"] == "waiting_rerun"
    assert len(fake_model.requests_of("term_list")) == 4
    assert REDACTED in final["error"], final["error"]
    assert FAKE_ARGON_API_KEY not in final["error"]
    task_dir = service.data_dir / "tasks" / task_id
    log = (task_dir / "task.log").read_text("utf-8")
    assert REDACTED in log, log
    files = [path for path in service.data_dir.rglob("*") if path.is_file()]
    names = {path.name for path in files}
    assert {"task.log", "report.json", "requests.jsonl"} <= names, names
    for path in files:
        for key in (FAKE_API_KEY, FAKE_ARGON_API_KEY):
            assert key.encode() not in path.read_bytes(), path
    for path in ("/health", f"/tasks/{task_id}"):
        for key in (FAKE_API_KEY, FAKE_ARGON_API_KEY):
            assert key.encode() not in service.http.get(path).content, path
    assert REDACTED in json.dumps(service.report(task_id), ensure_ascii=False)


# -- M4 ticket 16 round 2: the data a rejection keeps from the answer ----------


@dataclasses.dataclass
class _Held:
    text: str
    more: list


class _RejectionWithParts(ValueError):
    """A check's rejection that carries parts of the answer, as terms.py and review.py's do."""

    def __init__(self, reason: str, parts: dict, held: _Held, *, cause: Exception):
        super().__init__(reason)
        self.parts = parts
        self.held = held
        self.pair = ("fine", f"pair {parts['text']}")
        self.__cause__ = cause


def test_the_parts_of_an_answer_a_rejection_keeps_are_scrubbed_of_the_key_too(tmp_path):
    key = FAKE_API_KEY
    with FakeModel() as fake:
        fake.respond("other", lambda body: f"meta-harness {key}")
        client = _client(fake, tmp_path)

        def validate(content: str) -> None:
            raise _RejectionWithParts(
                "还有条目不合格",
                {"text": content, 1: [f"nested {content}", {"deep": content}], "key": "plain"},
                _Held(content, [content, (content,)]),
                cause=KeyError(f"原文 {content}"),
            )

        try:
            with pytest.raises(ModelCallError) as failure:
                client.complete([{"role": "user", "content": "hi"}], validate=validate)
        finally:
            client.close()

    assert len(failure.value.rejections) == 2
    for rejection in failure.value.rejections:
        assert isinstance(rejection, _RejectionWithParts)
        kept = repr((rejection.args, rejection.parts, rejection.held, rejection.pair, str(rejection.__cause__)))
        assert key not in kept, kept
        assert rejection.parts["text"] == f"meta-harness {REDACTED}"
        assert rejection.parts[1] == [f"nested meta-harness {REDACTED}", {"deep": f"meta-harness {REDACTED}"}]
        assert rejection.parts["key"] == "plain"
        assert rejection.held == _Held(f"meta-harness {REDACTED}", [f"meta-harness {REDACTED}", (f"meta-harness {REDACTED}",)])
        assert rejection.pair == ("fine", f"pair meta-harness {REDACTED}")


def test_a_review_answer_echoing_the_key_in_a_kept_verdict_leaves_it_out_of_the_result_and_the_report(tmp_path):
    items = [
        {"id": 0, "source": "It has 3 parts.", "draft": "它有几个部分。", "page": 2},
        {"id": 1, "source": "It runs 7 steps.", "draft": "它运行几步。", "page": 2},
    ]
    # Item 0 passes the checks with the key in its text; item 1 fails them, so the answer is rejected.
    answer = [{"id": 0, "output": f"它分 3 个部分，{FAKE_API_KEY}"}, {"id": 1, "output": "它运行几步。"}]
    with FakeModel() as fake:
        fake.respond("review", lambda body: json.dumps(answer, ensure_ascii=False))
        client = _client(fake, tmp_path)
        try:
            reviewer = BatchReviewer(client, FAKE_DOSSIER, KeptSource())
            result = reviewer(items, [])
        finally:
            client.close()

    assert result[1] is None
    assert result[0] == f"它分 3 个部分，{REDACTED}"
    assert FAKE_API_KEY not in json.dumps([result, reviewer.report(), reviewer.kept.paragraphs], ensure_ascii=False)


# -- M4 ticket 17: a key echoed in an answer that passes validation ---------------


def _cache_entries(directory: Path) -> list[dict]:
    return [json.loads(path.read_text("utf-8")) for path in sorted(directory.rglob("*.json"))]


def test_a_validated_answer_echoing_the_key_is_returned_checked_and_cached_without_it(tmp_path):
    echoed = f"meta-harness {FAKE_API_KEY} 回显"
    checked = []
    with FakeModel() as fake:
        fake.respond("other", lambda body: echoed)
        client = _client(fake, tmp_path)
        try:
            content = client.complete([{"role": "user", "content": "hi"}], validate=checked.append)
            # The same request again: the cache answers, with what was saved.
            again = client.complete([{"role": "user", "content": "hi"}], validate=checked.append)
        finally:
            client.close()

    expected = f"meta-harness {REDACTED} 回显"
    assert content == expected and again == expected
    assert checked == [expected]  # the check sees the answer as it is kept, once (the second was a hit)
    assert len(fake.requests) == 1
    entries = _cache_entries(tmp_path / "cache")
    assert [e["content"] for e in entries] == [expected]
    for path in (tmp_path / "cache").rglob("*"):
        if path.is_file():
            assert FAKE_API_KEY.encode() not in path.read_bytes(), path


def test_an_answer_without_the_key_is_returned_and_cached_exactly_as_it_came(tmp_path):
    # Whitespace inside, a near miss of the key, the other endpoint's key shape:
    # nothing here is the key, so nothing changes.
    answer = f'{{"terms": ["a  b", "{FAKE_API_KEY[:-1]}", "sk-other"]}}\n\n  tail 回答'
    with FakeModel() as fake:
        fake.respond("other", lambda body: answer)
        client = _client(fake, tmp_path)
        try:
            content = client.complete([{"role": "user", "content": "hi"}], validate=lambda c: None)
            assert client._redact(answer) == answer
        finally:
            client.close()

    assert content == answer.strip()
    assert [e["content"] for e in _cache_entries(tmp_path / "cache")] == [answer.strip()]


def test_a_review_answer_echoing_the_key_that_passes_its_checks_leaves_it_out_of_the_result_and_the_report(
    tmp_path,
):
    items = [
        {"id": 0, "source": "It has 3 parts.", "draft": "它有几个部分。", "page": 2},
        {"id": 1, "source": "It runs 7 steps.", "draft": "它运行几步。", "page": 2},
    ]
    # Both items pass the checks; item 0 has the key in its text.
    answer = [
        {"id": 0, "output": f"它分 3 个部分，{FAKE_API_KEY}"},
        {"id": 1, "output": "它运行 7 步。"},
    ]
    with FakeModel() as fake:
        fake.respond("review", lambda body: json.dumps(answer, ensure_ascii=False))
        client = _client(fake, tmp_path)
        try:
            reviewer = BatchReviewer(client, FAKE_DOSSIER, KeptSource())
            result = reviewer(items, [])
        finally:
            client.close()

    assert result == {0: f"它分 3 个部分，{REDACTED}", 1: "它运行 7 步。"}
    assert FAKE_API_KEY not in json.dumps([result, reviewer.report(), reviewer.kept.paragraphs], ensure_ascii=False)
    cached = "".join(e["content"] for e in _cache_entries(tmp_path / "cache"))
    assert FAKE_API_KEY not in cached and REDACTED in cached
