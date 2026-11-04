"""Cancelling a running task; retrying a failed reap (M3 ticket 09; spec sections 1, 2 and 4, seam 1 "取消与进程").

``POST /tasks/{id}/cancel`` on a running task first saves that the cancel was
accepted, then reaps every process with the task's marker, and only then
makes the task ``cancelled`` (200); queries are answered meanwhile. When the
reaping fails it answers 503, the task keeps its state with ``cleanup_error``,
and no new attempt starts; the service tries the reap again every 30
seconds (and at once on a repeated cancel), and once it succeeds completes
the transition it was about to make. A result the worker reports after the
cancel is dropped. A restart while the processes are being reaped completes
the cancel, counting no interruption and no rerun.

Only the HTTP interface, the service log, the task report and the process
table are looked at. Like ``test_requeue``, two tests kill a worker directly
to leave its spawned child behind; there is no other way to stage that.
"""

import json
import threading
import time

import httpx
import psutil

from tests.conftest import launch_service
from tests.conftest import marked_processes
from tests.conftest import stop_service
from tests.fake_model import FAKE_DOSSIER
from tests.fake_model import FakeModel
from tests.test_cancel import PAUSE_AFTER_CANCEL_ACCEPTED
from tests.test_cancel import PAUSE_AFTER_MARKER
from tests.test_cancel import SCAN_ERROR
from tests.test_cancel import WAITS
from tests.test_cancel import _assert_cancelled
from tests.test_cancel import _cancel
from tests.test_cancel import _events
from tests.test_cancel import _kill
from tests.test_cancel import _leftover
from tests.test_cancel import _pauses
from tests.test_cancel import _wait_for_file

PAUSE_BEFORE_ADOPT = "ZT_TEST_PAUSE_BEFORE_ADOPT"
SPAWN_CHILD = "ZT_TEST_WORKER_SPAWN_CHILD"
REAP_RETRY_SECONDS = 30


def _hold_dossier(model: FakeModel) -> threading.Event:
    """Dossier requests wait until the returned event is set: the attempt stays running."""
    gate = threading.Event()

    def answer(body):
        gate.wait(600)
        return json.dumps(FAKE_DOSSIER, ensure_ascii=False)

    model.respond("dossier", answer)
    return gate


def _worker_and_children(service, task_id: str) -> tuple[psutil.Process, list[psutil.Process]]:
    """Wait until the worker has spawned its child; return the worker and its other marked processes."""
    deadline = time.monotonic() + 120
    while True:
        marked = marked_processes([task_id])
        workers = [p for p in marked if p.ppid() == service.process.pid]
        others = [p for p in marked if p.ppid() != service.process.pid]
        if workers and others:
            return workers[0], others
        assert time.monotonic() < deadline, "the worker never spawned its child"
        time.sleep(0.1)


def _marked_while_polling(task_id: str) -> list[psutil.Process]:
    """``marked_processes``, tolerating psutil's SystemError for a process that exits mid-read."""
    for _ in range(5):
        try:
            return marked_processes([task_id])
        except SystemError:
            time.sleep(0.05)
    return marked_processes([task_id])


def _attempt_outcomes(service, task_id: str) -> list[str]:
    return [a["outcome"] for a in service.report(task_id)["attempts"]]


def test_a_running_task_with_a_spawned_child_is_cancelled_and_leaves_no_process(
    tmp_path, fake_model, start_service
):
    accepted = _pauses(tmp_path, "accepted")
    gate = _hold_dossier(fake_model)
    service = start_service(fake_model, env={SPAWN_CHILD: "1", PAUSE_AFTER_CANCEL_ACCEPTED: str(accepted)})
    try:
        task_id = service.submit()
        worker, children = _worker_and_children(service, task_id)
        assert service.status(task_id)["state"] == "running"

        answers = []
        thread = threading.Thread(target=lambda: answers.append(_cancel(service, task_id)), daemon=True)
        thread.start()
        paused = _wait_for_file(accepted / f"{task_id}-1.paused")
        # The cancel is saved first; queries are answered while it waits.
        during = service.status(task_id)
        assert during["state"] == "running"
        assert during["cancel_requested"] is True
        assert service.http.get("/health").status_code == 200
        started = time.monotonic()
        paused.unlink()
        thread.join(30)
        assert time.monotonic() - started < 15  # within the reaping rule's bound

        body = _assert_cancelled(answers[0], service, task_id)
        assert body["attempt"] == 1
        assert body["reruns_used"] == 0
        assert body["cleanup_error"] is None
        assert not worker.is_running() or worker.status() == psutil.STATUS_ZOMBIE
        assert marked_processes([task_id]) == []
        assert all(not c.is_running() or c.status() == psutil.STATUS_ZOMBIE for c in children)
        assert _events(service, task_id) == ["提交", "开始", "接受取消", "已取消"]
        assert _attempt_outcomes(service, task_id) == ["cancelled"]
        assert service.report(task_id)["complete"] is False
        again = _cancel(service, task_id)
        assert again.status_code == 200
        assert again.json() == body

        # The queue goes on.
        gate.set()
        other = service.submit()
        assert service.wait(other)["state"] == "succeeded"
        assert service.status(task_id)["state"] == "cancelled"
        assert _events(service, task_id) == ["提交", "开始", "接受取消", "已取消"]
    finally:
        gate.set()


def test_a_task_cancelled_after_its_marker_was_saved_never_starts_its_worker(
    tmp_path, fake_model, start_service
):
    pauses = _pauses(tmp_path)
    service = start_service(fake_model, env={PAUSE_AFTER_MARKER: str(pauses)})

    task_id = service.submit()
    paused = _wait_for_file(pauses / f"{task_id}-1.paused")
    assert service.status(task_id)["state"] == "running"
    _assert_cancelled(_cancel(service, task_id), service, task_id)
    paused.unlink()

    other = service.submit()
    seen = []
    # The other task waits at the same pause point; meanwhile, and while it
    # runs, no process of the cancelled task may show up.
    other_paused = pauses / f"{other}-1.paused"
    deadline = time.monotonic() + 120
    while not other_paused.exists():
        assert time.monotonic() < deadline, "the other task never reached its pause"
        seen.extend(_marked_while_polling(task_id))
        time.sleep(0.1)
    other_paused.unlink()
    service.wait(other, on_poll=lambda s: seen.extend(_marked_while_polling(task_id)))
    assert service.status(other)["state"] == "succeeded"
    assert seen == [], "the cancelled task's worker started"
    assert service.status(task_id)["state"] == "cancelled"
    assert _events(service, task_id) == ["提交", "开始", "接受取消", "已取消"]
    assert _attempt_outcomes(service, task_id) == ["cancelled"]


def test_a_success_reported_after_the_cancel_is_dropped(tmp_path, fake_model, start_service):
    # The worker has reported success and exited; the service is held before
    # it adopts the result, and the cancel comes in between.
    pauses = _pauses(tmp_path)
    service = start_service(fake_model, env={PAUSE_BEFORE_ADOPT: str(pauses)})

    task_id = service.submit()
    paused = _wait_for_file(pauses / f"{task_id}-1.paused", timeout=300)
    assert service.status(task_id)["state"] == "running"
    body = _assert_cancelled(_cancel(service, task_id), service, task_id)
    paused.unlink()
    other = service.submit()
    _wait_for_file(pauses / f"{other}-1.paused", timeout=300).unlink()
    assert service.wait(other)["state"] == "succeeded"

    after = service.status(task_id)
    assert after == body  # state and counters unchanged
    assert after["state"] == "cancelled"
    assert after["attempt"] == 1
    assert after["reruns_used"] == 0
    assert _events(service, task_id) == ["提交", "开始", "接受取消", "已取消"]
    assert _attempt_outcomes(service, task_id) == ["cancelled"]


def test_a_restart_while_a_running_tasks_processes_are_reaped_completes_the_cancel(tmp_path):
    accepted = _pauses(tmp_path, "accepted")
    with FakeModel() as model:
        gate = _hold_dossier(model)
        services = [launch_service(model, tmp_path, env={PAUSE_AFTER_CANCEL_ACCEPTED: str(accepted)})]
        leftover = None
        try:
            task_id = services[0].submit()
            services[0].wait_for(task_id, lambda s: s["state"] == "running" and len(model.requests_of("dossier")) > 0)
            leftover = _leftover(task_id)  # a process the restarted service must reap
            threading.Thread(target=_cancel_ignoring_errors, args=(services[0], task_id), daemon=True).start()
            _wait_for_file(accepted / f"{task_id}-1.paused")
            during = services[0].status(task_id)
            assert (during["state"], during["cancel_requested"]) == ("running", True)
            _kill(services[0])

            gate.set()
            services.append(launch_service(model, tmp_path, data_dir=services[0].data_dir))
            cancelled = services[1].status(task_id)
            assert cancelled["state"] == "cancelled"
            assert cancelled["cancel_requested"] is False
            assert cancelled["attempt"] == 1
            assert cancelled["reruns_used"] == 0
            assert leftover.poll() is not None, "the leftover process outlived the restart"
            assert marked_processes([task_id]) == []
            assert _events(services[1], task_id) == ["已取消"]
            # No interruption counted: the attempt ended as cancelled.
            assert _attempt_outcomes(services[1], task_id) == ["cancelled"]

            other = services[1].submit()
            assert services[1].wait(other)["state"] == "succeeded"
            assert services[1].status(task_id)["state"] == "cancelled"
        finally:
            gate.set()
            for service in reversed(services):
                stop_service(service)
            if leftover is not None:
                leftover.kill()
                leftover.wait()


def test_a_running_cancel_whose_reap_fails_answers_503_and_the_retry_completes_it(
    tmp_path, fake_model, start_service
):
    scan_error = tmp_path / "scan-error"
    gate = _hold_dossier(fake_model)
    service = start_service(fake_model, env={SCAN_ERROR: str(scan_error)})
    try:
        task_id = service.submit()
        service.wait_for(task_id, lambda s: s["state"] == "running" and len(fake_model.requests_of("dossier")) > 0)
        scan_error.touch()  # from now on the process scan itself fails
        response = _cancel(service, task_id)

        assert response.status_code == 503, response.text
        body = response.json()
        assert body == service.status(task_id)
        assert body["state"] == "running"
        assert body["cancel_requested"] is True
        assert "无法扫描进程" in body["cleanup_error"], body["cleanup_error"]
        assert _events(service, task_id) == ["提交", "开始", "接受取消", "进程回收出错"]
        # A repeated cancel tries again at once: still failing, still 503.
        assert _cancel(service, task_id).status_code == 503
        # No new attempt starts while a reap has failed.
        other = service.submit()
        time.sleep(2)
        assert service.status(other)["state"] == "queued"
        assert service.status(other)["attempt"] == 0

        # No further cancel: the service's own retry completes it.
        scan_error.unlink()
        cleared = time.monotonic()
        final = service.wait_for(task_id, lambda s: s["state"] == "cancelled", timeout=REAP_RETRY_SECONDS + 15)
        assert time.monotonic() - cleared <= REAP_RETRY_SECONDS + 15
        assert final["cleanup_error"] is None
        assert final["cancel_requested"] is False
        assert marked_processes([task_id]) == []
        assert _events(service, task_id)[-1] == "已取消"
        assert _attempt_outcomes(service, task_id) == ["cancelled"]

        gate.set()
        assert service.wait(other)["state"] == "succeeded"
    finally:
        gate.set()


def test_a_worker_that_exits_without_a_result_stays_running_until_its_processes_are_reaped(
    tmp_path, fake_model, start_service
):
    # The worker is killed, leaving its spawned child; reaping fails. The
    # task keeps its state until the retry reaps the child, and only then
    # fails the attempt (retryable: it waits for a rerun).
    scan_error = tmp_path / "scan-error"
    gate = _hold_dossier(fake_model)
    service = start_service(fake_model, env={SPAWN_CHILD: "1", SCAN_ERROR: str(scan_error), WAITS: "600,600"})
    try:
        task_id = service.submit()
        worker, children = _worker_and_children(service, task_id)
        scan_error.touch()
        worker.kill()

        held = service.wait_for(task_id, lambda s: s["cleanup_error"] is not None, timeout=60)
        assert held["state"] == "running"
        assert "无法扫描进程" in held["cleanup_error"]
        assert all(c.is_running() for c in children), "the child should still be there"
        other = service.submit()
        time.sleep(2)
        assert service.status(task_id)["state"] == "running"
        assert service.status(other)["attempt"] == 0

        scan_error.unlink()
        waiting = service.wait_for(
            task_id, lambda s: s["state"] == "waiting_rerun", timeout=REAP_RETRY_SECONDS + 15
        )
        assert "论文任务进程异常退出" in waiting["error"]
        assert waiting["cleanup_error"] is None
        assert marked_processes([task_id]) == []
        assert _events(service, task_id) == ["提交", "开始", "进程回收出错", "等待自动重跑"]

        gate.set()
        assert service.wait(other)["state"] == "succeeded"
    finally:
        gate.set()


def _cancel_ignoring_errors(service, task_id: str) -> None:
    try:
        _cancel(service, task_id)
    except httpx.HTTPError:  # the service is killed while the cancel waits
        pass

