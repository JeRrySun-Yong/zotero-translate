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
"""

import collections
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from zotero_translate.paper_task import DOSSIER_STAGE

from tests.conftest import EXCERPT
from tests.conftest import FAKE_API_KEY
from tests.conftest import FAKE_ARGON_API_KEY
from tests.conftest import FAKE_MODEL_NAME
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.conftest import write_env_file
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

    first = service.wait(service.submit())

    assert first["state"] == "failed"
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

    final = service.wait(service.submit())

    assert final["state"] == "failed"
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

    final = service.wait(service.submit())

    assert final["state"] == "failed"
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
    final = service.wait(task_id)

    assert final["state"] == "failed"
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
    final = service.wait(service.submit(), timeout=180)

    assert final["state"] == "failed"
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
