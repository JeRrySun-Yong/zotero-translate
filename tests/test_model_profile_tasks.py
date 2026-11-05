"""Paper tasks and model profiles through the translation service (M4 ticket 02).

A task records the current profile when it is submitted and keeps it for
every attempt: a requeue after a restart and an automatic rerun use it
too, whatever profile is current by then. A user profile's two roles share
its one endpoint: one budget of the profile's concurrency, no hedging, and
a line in the task log that says so. Which endpoint each request went to is
read from the fake models' request logs and the attempts' request records.
"""

import json
from pathlib import Path

from tests.conftest import ARGON_MODEL_NAME
from tests.conftest import FAKE_MODEL_NAME
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FakeModel
from zotero_translate.request_log import REQUESTS_FILE
from zotero_translate.request_log import load_records
from zotero_translate.settings import HEDGE_AFTER_SETTING

HEADERS = {"X-Zotero-Translate": "1"}
KEY = "sk-profile-secret-key-4321"
PROFILE_MODEL = "fake-deepseek-v4.1-flash"
GEMINI_MODELS = {FAKE_MODEL_NAME, ARGON_MODEL_NAME}


def _apply_new_profile(service, model: FakeModel, **fields) -> dict:
    body = {"name": "DeepSeek", "base_url": model.base_url, "api_key": KEY, "model": PROFILE_MODEL, **fields}
    created = service.http.post("/profiles", json=body, headers=HEADERS)
    assert created.status_code == 201, created.text
    profile = created.json()
    tested = service.http.post(f"/profiles/{profile['id']}/test", json={}, headers=HEADERS)
    assert tested.json()["ok"], tested.text
    applied = service.http.post(f"/profiles/{profile['id']}/apply", json={}, headers=HEADERS)
    assert applied.status_code == 200, applied.text
    return profile


def _records(service, task_id: str, attempt: int) -> list[dict]:
    path = Path(service.data_dir) / "tasks" / task_id / "attempts" / str(attempt) / REQUESTS_FILE
    return load_records(path)


def _paper_requests(model: FakeModel) -> list:
    return [r for r in model.requests if r.kind != "connection_test"]


def test_a_task_submitted_after_apply_uses_the_profile_with_one_shared_budget(start_service):
    with FakeModel(delay=0.05) as gemini, FakeModel(delay=0.05) as deepseek:
        service = start_service(gemini, env={HEDGE_AFTER_SETTING: "1"})
        profile = _apply_new_profile(service, deepseek, concurrency=3)
        task_id = service.submit()
        final = service.wait(task_id)
        report_text = (service.data_dir / "tasks" / task_id / "report.json").read_text("utf-8")
        log = (service.data_dir / "tasks" / task_id / "task.log").read_text("utf-8")

    assert final["state"] == "succeeded", final["error"]
    assert final["profile"] == {"id": profile["id"], "name": "DeepSeek"}
    assert gemini.requests == []
    paper = _paper_requests(deepseek)
    assert len(paper) > 2
    assert {r.body["model"] for r in paper} == {PROFILE_MODEL}
    assert {r.headers["Authorization"] for r in paper} == {f"Bearer {KEY}"}
    # The connection test sent what a paper request sends, but the messages.
    [check] = deepseek.requests_of("connection_test")
    batch = deepseek.requests_of("translate_batch")[0]
    assert {k: v for k, v in check.body.items() if k != "messages"} == {
        k: v for k, v in batch.body.items() if k != "messages"
    }
    # Both roles on one endpoint: one budget of the profile's 3, no hedging.
    assert deepseek.max_in_flight <= 3
    report = json.loads(report_text)
    assert report["profile"] == {
        "id": profile["id"],
        "name": "DeepSeek",
        "models": {"flash": PROFILE_MODEL, "argon": PROFILE_MODEL},
    }
    assert report["model"]["shared_budget"]["concurrency"] == 3
    assert report["model"]["hedging"] == {"after_seconds": None, "copies": 0}
    assert "share one budget of 3 requests in flight" in log
    assert KEY not in report_text and KEY not in log and KEY not in "\n".join(service.output)


# M4 ticket 12: a profile with streaming on streams every request of its tasks.
def test_a_task_of_a_streaming_profile_streams_every_request_and_records_them(start_service):
    with FakeModel() as gemini, FakeModel() as deepseek:
        service = start_service(gemini)
        _apply_new_profile(service, deepseek, stream=True)
        task_id = service.submit()
        final = service.wait(task_id)
        report = json.loads((service.data_dir / "tasks" / task_id / "report.json").read_text("utf-8"))

    assert final["state"] == "succeeded", final["error"]
    paper = _paper_requests(deepseek)
    assert len(paper) > 2
    assert all(r.body["stream"] is True for r in paper)
    endpoints = report["model"]["endpoints"]
    assert endpoints["flash"]["stream"] is endpoints["argon"]["stream"] is True
    records = _records(service, task_id, 1)
    sent = [r for r in records if r["result"] != "cache_hit"]
    assert len(sent) == len(paper)
    assert {(r["result"], r["http_status"]) for r in sent} == {("succeeded", 200)}


def test_a_gemini_task_reports_streaming_off(start_service):
    with FakeModel() as gemini:
        service = start_service(gemini)
        task_id = service.submit()
        final = service.wait(task_id)
        report = json.loads((service.data_dir / "tasks" / task_id / "report.json").read_text("utf-8"))

    assert final["state"] == "succeeded", final["error"]
    assert all("stream" not in r.body for r in gemini.requests)
    endpoints = report["model"]["endpoints"]
    assert endpoints["flash"]["stream"] is endpoints["argon"]["stream"] is False


def test_tasks_keep_the_profile_they_were_submitted_with(tmp_path):
    """Applied while one task runs and one is queued: both, their requeue
    after a restart and their automatic rerun stay on Gemini; the task
    submitted after it goes to the profile."""
    pause = tmp_path / "pause"
    pause.mkdir()
    with FakeModel() as gemini, FakeModel() as deepseek:
        # The running task's next attempt fails (retryable) and is rerun.
        gemini.fail_same_request(["500"] * 4, kind="dossier")
        first = launch_service(gemini, tmp_path, env={"ZT_TEST_PAUSE_AFTER_MARKER": str(pause)})
        try:
            running = first.submit()
            assert first.wait_for(running, lambda s: s["state"] == "running", 30)
            queued = first.submit()
            profile = _apply_new_profile(first, deepseek)
            after = first.submit()
            statuses = {t: first.status(t) for t in (running, queued, after)}
        finally:
            stop_service(first)  # the running task is interrupted
        assert gemini.requests == [] and _paper_requests(deepseek) == []

        service = launch_service(
            gemini,
            tmp_path,
            data_dir=first.data_dir,
            env={"ZT_TEST_RERUN_WAITS_SECONDS": "1,1"},
        )
        try:
            assert service.http.get("/profiles").json()["current"] == profile["id"]
            finals = {t: service.wait(t, timeout=600) for t in (running, queued, after)}
        finally:
            stop_service(service)

    gemini_profile = {"id": "gemini", "name": "Gemini"}
    assert statuses[running]["profile"] == statuses[queued]["profile"] == gemini_profile
    assert statuses[after]["profile"] == {"id": profile["id"], "name": "DeepSeek"}
    assert all(f["state"] == "succeeded" for f in finals.values()), finals
    # The running task: interrupted (1), requeued and failed (2), rerun (3).
    assert finals[running]["attempt"] == 3 and finals[running]["reruns_used"] == 1
    events = "\n".join(service.log_lines(running))
    assert "event=重新排队并开始新尝试" in events and "event=自动重跑开始" in events
    for task_id, attempts in ((running, (2, 3)), (queued, (1,))):
        for attempt in attempts:
            records = _records(service, task_id, attempt)
            assert records and {r["model"] for r in records} <= GEMINI_MODELS, (task_id, attempt)
    records = _records(service, after, 1)
    assert records and {r["model"] for r in records} == {PROFILE_MODEL}
    # The fake models' own logs agree: each endpoint got only its models.
    assert gemini.requests and {r.body["model"] for r in gemini.requests} <= GEMINI_MODELS
    assert {r.body["model"] for r in _paper_requests(deepseek)} == {PROFILE_MODEL}
    assert service.report(running)["profile"]["id"] == "gemini"
    assert service.report(after)["profile"]["models"] == {"flash": PROFILE_MODEL, "argon": PROFILE_MODEL}
