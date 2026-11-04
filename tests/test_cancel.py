"""Cancelling queued and waiting tasks (M3 ticket 08; spec sections 1, 4 and 5, seam 1 "取消与进程").

``POST /tasks/{id}/cancel`` first saves that the cancel was accepted, then
reaps every process with the task's marker, and only then cancels a queued
task or one waiting for an automatic rerun; when the reaping fails it answers
503 with the reason, and a repeated cancel tries again. A cancelled
task is never started: not when its rerun comes due, not when it reaches the
head of the queue, not after a restart; a restart completes an accepted
cancel. Cancelling again answers 200; a finished task answers 409, an
unknown one 404. Until ticket 09, a running task also answers 409.

Only the HTTP interface, the service log, the status files and the process
table are looked at.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from tests.conftest import MARK_ENV
from tests.conftest import launch_service
from tests.conftest import marked_processes
from tests.conftest import stop_service
from tests.fake_model import FAKE_DOSSIER
from tests.fake_model import FakeModel

WAITS = "ZT_TEST_RERUN_WAITS_SECONDS"
PAUSE_AFTER_MARKER = "ZT_TEST_PAUSE_AFTER_MARKER"
PAUSE_BEFORE_RERUN_QUEUE = "ZT_TEST_PAUSE_BEFORE_RERUN_QUEUE"
PAUSE_BEFORE_REAP = "ZT_TEST_PAUSE_BEFORE_REAP"
PAUSE_AFTER_CANCEL_ACCEPTED = "ZT_TEST_PAUSE_AFTER_CANCEL_ACCEPTED"
SCAN_ERROR = "ZT_TEST_REAP_SCAN_ERROR"


def _event(line: str) -> str:
    return line.split(" event=", 1)[1].split(" ", 1)[0]


def _events(service, task_id: str) -> list[str]:
    return [_event(line) for line in service.log_lines(task_id)]


def _wait_for_file(path: Path, timeout: float = 120) -> Path:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.05)
    return path


def _pauses(tmp_path: Path, name: str = "pauses") -> Path:
    directory = tmp_path / name
    directory.mkdir()
    return directory


def _status_path(service, task_id: str) -> Path:
    return service.data_dir / "tasks" / task_id / "status.json"


def _kill(service) -> None:
    """Kill the service with SIGKILL, as a crash would."""
    service.process.kill()
    service.process.wait(timeout=10)


def _cancel(service, task_id: str):
    return service.http.post(f"/tasks/{task_id}/cancel")


def _fail_dossier(model: FakeModel) -> None:
    """Every dossier answer is rejected: an attempt fails, retryable, after 4 sends."""
    model.respond("dossier", lambda body: "not a dossier")


def _answer_dossier(model: FakeModel) -> None:
    model.respond("dossier", lambda body: json.dumps(FAKE_DOSSIER, ensure_ascii=False))


def _assert_cancelled(response, service, task_id: str) -> dict:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == service.status(task_id)  # the same object as a query
    assert body["state"] == "cancelled"
    assert body["cancel_requested"] is False  # true only while the cancel is under way
    assert body["queue_position"] is None
    assert body["next_attempt_in_seconds"] is None
    assert body["error"] is None
    return body


def test_a_queued_task_is_cancelled_never_starts_and_a_second_cancel_answers_200(
    tmp_path, fake_model, start_service
):
    pauses = _pauses(tmp_path)
    service = start_service(fake_model, env={PAUSE_AFTER_MARKER: str(pauses)})

    a, b, c = service.submit(), service.submit(), service.submit()
    paused_a = _wait_for_file(pauses / f"{a}-1.paused")  # a holds the queue
    assert service.status(b)["queue_position"] == 1
    assert service.status(b)["cancel_requested"] is False

    first = _assert_cancelled(_cancel(service, b), service, b)
    assert first["attempt"] == 0
    assert service.status(c)["queue_position"] == 1
    again = _cancel(service, b)
    assert again.status_code == 200
    assert again.json() == first

    paused_a.unlink()
    _wait_for_file(pauses / f"{c}-1.paused", timeout=300).unlink()
    assert service.wait(a)["state"] == "succeeded"
    assert service.wait(c)["state"] == "succeeded"
    assert not (pauses / f"{b}-1.paused").exists()
    assert service.status(b)["state"] == "cancelled"
    assert service.status(b)["attempt"] == 0
    assert _events(service, b) == ["提交", "接受取消", "已取消"]


def test_a_task_waiting_for_a_rerun_is_cancelled_and_not_rerun_when_due(fake_model, start_service):
    _fail_dossier(fake_model)
    service = start_service(fake_model, env={WAITS: "2,600"})

    task_id = service.submit()
    waiting = service.wait_for(task_id, lambda s: s["state"] == "waiting_rerun", timeout=120)
    assert waiting["next_attempt_in_seconds"] is not None
    _assert_cancelled(_cancel(service, task_id), service, task_id)
    _answer_dossier(fake_model)
    time.sleep(3)  # past the due time

    final = service.status(task_id)
    assert final["state"] == "cancelled"
    assert final["attempt"] == 1
    assert final["reruns_used"] == 0
    assert _events(service, task_id) == ["提交", "开始", "等待自动重跑", "接受取消", "已取消"]
    # The queue goes on.
    other = service.submit()
    assert service.wait(other)["state"] == "succeeded"


def test_a_cancel_that_collides_with_the_rerun_coming_due_wins(
    tmp_path, fake_model, start_service
):
    # The rerun timer is held after the rerun came due, before it queues the
    # task; the cancel comes in between.
    due = _pauses(tmp_path)
    _fail_dossier(fake_model)
    service = start_service(fake_model, env={WAITS: "0.5,600", PAUSE_BEFORE_RERUN_QUEUE: str(due)})

    task_id = service.submit()
    paused = _wait_for_file(due / f"{task_id}-1.paused", timeout=120)
    assert service.status(task_id)["state"] == "waiting_rerun"
    _assert_cancelled(_cancel(service, task_id), service, task_id)
    _answer_dossier(fake_model)
    paused.unlink()
    other = service.submit()
    assert service.wait(other)["state"] == "succeeded"

    final = service.status(task_id)
    assert final["state"] == "cancelled"
    assert final["attempt"] == 1
    assert final["reruns_used"] == 0
    assert _events(service, task_id) == ["提交", "开始", "等待自动重跑", "接受取消", "已取消"]


def test_a_task_cancelled_after_the_queue_picked_it_does_not_start(
    tmp_path, fake_model, start_service
):
    # The runner has picked the task and is about to reap its earlier
    # processes when the cancel comes in.
    reaps = _pauses(tmp_path)
    service = start_service(fake_model, env={PAUSE_BEFORE_REAP: str(reaps)})

    task_id = service.submit()
    paused = _wait_for_file(reaps / f"{task_id}-1.paused")
    _assert_cancelled(_cancel(service, task_id), service, task_id)
    paused.unlink()
    other = service.submit()
    _wait_for_file(reaps / f"{other}-1.paused", timeout=60).unlink()
    assert service.wait(other)["state"] == "succeeded"

    assert service.status(task_id)["state"] == "cancelled"
    assert service.status(task_id)["attempt"] == 0
    assert _events(service, task_id) == ["提交", "接受取消", "已取消"]


def test_finished_and_running_tasks_answer_409_unchanged_and_unknown_ones_404(
    tmp_path, fake_model, start_service
):
    pauses = _pauses(tmp_path)
    fake_model.inject_error("401", kind="dossier")  # the first task fails at once
    service = start_service(fake_model, env={PAUSE_AFTER_MARKER: str(pauses)})

    failed = service.submit()
    _wait_for_file(pauses / f"{failed}-1.paused").unlink()
    assert service.wait(failed, timeout=600)["state"] == "failed"
    succeeded = service.submit()
    _wait_for_file(pauses / f"{succeeded}-1.paused").unlink()
    assert service.wait(succeeded, timeout=600)["state"] == "succeeded"
    running = service.submit()
    _wait_for_file(pauses / f"{running}-1.paused", timeout=600)  # held: it stays running

    for task_id, state in ((failed, "failed"), (succeeded, "succeeded"), (running, "running")):
        before = service.status(task_id)
        response = _cancel(service, task_id)
        assert response.status_code == 409, (state, response.text)
        assert response.json() == before
        assert response.json()["state"] == state
        assert response.json()["cancel_requested"] is False
        assert service.status(task_id) == before
        assert "接受取消" not in _events(service, task_id)
    assert _cancel(service, "0" * 32).status_code == 404
    assert _cancel(service, "not-a-task").status_code == 404


def test_a_cancelled_task_is_not_queued_again_after_a_restart(tmp_path):
    pauses = _pauses(tmp_path)
    env = {PAUSE_AFTER_MARKER: str(pauses)}
    with FakeModel() as model:
        services = [launch_service(model, tmp_path, env=env)]
        try:
            a, b = services[0].submit(), services[0].submit()
            _wait_for_file(pauses / f"{a}-1.paused")  # a holds the queue
            _assert_cancelled(_cancel(services[0], b), services[0], b)

            _kill(services[0])
            services.append(launch_service(model, tmp_path, data_dir=services[0].data_dir, env=env))
            _wait_for_file(pauses / f"{a}-2.paused").unlink()
            assert services[1].wait(a)["state"] == "succeeded"

            assert services[1].status(b)["state"] == "cancelled"
            assert services[1].status(b)["attempt"] == 0
            assert not (pauses / f"{b}-1.paused").exists()
            assert _events(services[1], b) == []
        finally:
            for service in reversed(services):
                stop_service(service)


def _leftover(task_id: str, attempt: int = 1) -> subprocess.Popen:
    """A process with the task's process marker that outlived its attempt.

    It stands for a child the engine spawned whose worker reported its
    result and was then killed before ending it: no service reaps such a
    child until the task's next attempt, and that window cannot be hit
    through the HTTP interface. ``stop_service`` ends it if a test does not.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3600)"],
        env={**os.environ, MARK_ENV: f"{task_id}:{attempt}"},
        start_new_session=True,
    )


def _wait_marked(task_id: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not marked_processes([task_id]):
        assert time.monotonic() < deadline, "the leftover process never showed up"
        time.sleep(0.05)


def test_a_cancel_reaps_the_tasks_leftover_processes_before_it_answers_200(
    tmp_path, fake_model, start_service
):
    _fail_dossier(fake_model)
    service = start_service(fake_model, env={WAITS: "600,600"})

    task_id = service.submit()
    service.wait_for(task_id, lambda s: s["state"] == "waiting_rerun", timeout=120)
    leftover = _leftover(task_id)
    _wait_marked(task_id)

    _assert_cancelled(_cancel(service, task_id), service, task_id)
    assert leftover.poll() is not None, "the leftover process outlived the cancel"
    assert marked_processes([task_id]) == []
    assert _events(service, task_id)[-2:] == ["接受取消", "已取消"]


def test_a_cancel_whose_reaping_fails_answers_503_and_a_repeated_cancel_completes_it(
    tmp_path, fake_model, start_service
):
    scan_error = tmp_path / "scan-error"
    _fail_dossier(fake_model)
    service = start_service(fake_model, env={WAITS: "600,600", SCAN_ERROR: str(scan_error)})

    task_id = service.submit()
    service.wait_for(task_id, lambda s: s["state"] == "waiting_rerun", timeout=120)
    scan_error.touch()  # from now on the process scan itself fails
    response = _cancel(service, task_id)

    assert response.status_code == 503, response.text
    body = response.json()
    assert body == service.status(task_id)
    assert body["state"] == "waiting_rerun"
    assert body["cancel_requested"] is True
    assert "无法扫描进程" in body["cleanup_error"], body["cleanup_error"]
    assert _events(service, task_id)[-2:] == ["接受取消", "进程回收出错"]
    # No new attempt starts while a reap has failed.
    other = service.submit()
    time.sleep(2)
    assert service.status(other)["state"] == "queued"
    assert service.status(other)["attempt"] == 0

    scan_error.unlink()
    again = _cancel(service, task_id)
    _assert_cancelled(again, service, task_id)
    assert again.json()["cleanup_error"] is None
    assert _events(service, task_id)[-3:] == ["接受取消", "进程回收出错", "已取消"]
    _answer_dossier(fake_model)
    assert service.wait(other)["state"] == "succeeded"


def test_cancelling_a_queued_task_whose_reap_failed_resumes_the_queue(
    tmp_path, fake_model, start_service
):
    # The queue's reap before the first attempt fails: the task stays
    # queued, with cleanup_error, and no attempt starts. A cancel reaps
    # again: 503 while that fails; once it succeeds, the task is cancelled
    # and the queue goes on.
    scan_error = tmp_path / "scan-error"
    scan_error.touch()
    service = start_service(fake_model, env={SCAN_ERROR: str(scan_error)})

    stuck = service.submit()
    held = service.wait_for(stuck, lambda s: s["cleanup_error"] is not None, timeout=120)
    assert held["state"] == "queued"
    other = service.submit()
    response = _cancel(service, stuck)
    assert response.status_code == 503, response.text
    assert response.json()["state"] == "queued"
    assert response.json()["cancel_requested"] is True
    assert "无法扫描进程" in response.json()["cleanup_error"]
    assert service.status(other)["attempt"] == 0

    scan_error.unlink()
    _assert_cancelled(_cancel(service, stuck), service, stuck)
    assert service.status(stuck)["cleanup_error"] is None
    assert service.wait(other, timeout=600)["state"] == "succeeded"
    assert service.status(stuck)["attempt"] == 0


def test_a_restart_completes_a_cancel_accepted_before_the_service_died(tmp_path):
    # The service dies after saving the accepted cancel, before reaping and
    # saving the cancelled state: one task queued, one waiting for a rerun,
    # each with a leftover process.
    pauses = _pauses(tmp_path)
    accepted = _pauses(tmp_path, "accepted")
    env = {WAITS: "600,600", PAUSE_AFTER_MARKER: str(pauses), PAUSE_AFTER_CANCEL_ACCEPTED: str(accepted)}
    with FakeModel() as model:
        _fail_dossier(model)
        services = [launch_service(model, tmp_path, env=env)]
        leftovers = []
        try:
            waiting, holder, queued = services[0].submit(), services[0].submit(), services[0].submit()
            _wait_for_file(pauses / f"{waiting}-1.paused").unlink()
            _wait_for_file(pauses / f"{holder}-1.paused")  # holder holds the queue
            services[0].wait_for(waiting, lambda s: s["state"] == "waiting_rerun", timeout=60)
            leftovers = [_leftover(waiting), _leftover(queued)]
            for task_id, attempt in ((waiting, 1), (queued, 0)):
                threading.Thread(target=_cancel_ignoring_errors, args=(services[0], task_id), daemon=True).start()
                _wait_for_file(accepted / f"{task_id}-{attempt}.paused")
                status = json.loads(_status_path(services[0], task_id).read_text("utf-8"))
                assert status["cancel_requested"] is True
                assert status["state"] != "cancelled"
            _kill(services[0])

            _answer_dossier(model)
            services.append(launch_service(model, tmp_path, data_dir=services[0].data_dir, env=env))
            for task_id in (waiting, queued):
                cancelled = services[1].status(task_id)
                assert cancelled["state"] == "cancelled"
                assert cancelled["cancel_requested"] is False
            assert [p.poll() is not None for p in leftovers] == [True, True]
            _wait_for_file(pauses / f"{holder}-2.paused").unlink()
            assert services[1].wait(holder)["state"] == "succeeded"

            assert services[1].status(waiting)["attempt"] == 1
            assert services[1].status(waiting)["reruns_used"] == 0
            assert services[1].status(queued)["attempt"] == 0
            for task_id in (waiting, queued):
                assert services[1].status(task_id)["state"] == "cancelled"
                assert _events(services[1], task_id) == ["已取消"]
                assert _cancel(services[1], task_id).status_code == 200
        finally:
            for service in reversed(services):
                stop_service(service)
            for leftover in leftovers:
                leftover.kill()
                leftover.wait()


def _cancel_ignoring_errors(service, task_id: str) -> None:
    try:
        _cancel(service, task_id)
    except httpx.HTTPError:  # the service is killed while the cancel waits
        pass
