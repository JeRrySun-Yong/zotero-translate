"""Requeue after a restart (M3 ticket 04; spec sections 1, 2, 3 and 5, seam 1).

When the translation service starts, a task an earlier service left running
was interrupted: it goes back to the queue, first, and starts a new attempt
under the same task ID, unless this was its third interruption in a row.
Queued tasks keep their place and count no interruption. Before a new
attempt and at startup, the processes of a task's earlier attempts are
reaped by their process marker. Calls answered before the interruption are
answered from the cache.

Only the HTTP interface, the service log, the status and report files, and
the process table are looked at.
"""

import json
import os
import shutil
import time
from pathlib import Path

import psutil

from tests.conftest import EXCERPT
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FakeModel
from tests.test_basic_term_promotion import HARNESS_GLOSS
from tests.test_basic_term_promotion import MODEL_TERMS
from tests.test_basic_term_promotion import rewriting_responder

MARK_ENV = "ZT_PROCESS_MARK"
RESTART_ERROR = "翻译服务重启，任务中断"
MANY_RESTARTS_ERROR = "翻译服务多次重启，任务中断"
UPGRADE_ERROR = "升级前未完成的任务，请重试"


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


def _status_file(service, task_id: str) -> dict:
    return json.loads((service.data_dir / "tasks" / task_id / "status.json").read_text("utf-8"))


def _marked(task_id: str) -> list[psutil.Process]:
    """Live processes whose process marker names ``task_id``."""
    found = []
    for process in psutil.process_iter():
        try:
            if process.environ().get(MARK_ENV, "").startswith(f"{task_id}:") and (
                process.status() != psutil.STATUS_ZOMBIE
            ):
                found.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return found


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _kill(service) -> None:
    """Kill the service with SIGKILL, as a crash would; its worker is left to itself."""
    service.process.kill()
    service.process.wait(timeout=10)


def _quiet(model: FakeModel, seconds: float = 2.0, timeout: float = 120) -> None:
    """Wait until the fake model has received no request for ``seconds``."""
    deadline = time.monotonic() + timeout
    while True:
        count = len(model.requests)
        time.sleep(seconds)
        if len(model.requests) == count:
            return
        assert time.monotonic() < deadline, "the task never stopped sending requests"


def test_a_task_running_when_the_service_dies_runs_again_as_attempt_2_and_succeeds(tmp_path):
    with FakeModel() as model:
        model.inject_error("timeout", kind="dossier")  # attempt 1 hangs on its dossier
        service = launch_service(model, tmp_path)
        restarted = None
        try:
            task_id = service.submit()
            running = service.wait_for(task_id, lambda s: s["state"] == "running" and model.requests)
            assert running["attempt"] == 1

            _kill(service)
            restarted = launch_service(model, tmp_path, data_dir=service.data_dir)
            final = restarted.wait(task_id)

            assert final["task_id"] == task_id
            assert final["state"] == "succeeded", final["error"]
            assert final["attempt"] == 2
            assert len(model.requests_of("dossier")) == 2
            assert _events(restarted, task_id) == [
                "中断",
                "重新排队并开始新尝试",
                "开始",
                "成功",
            ]
            interrupted = restarted.log_lines(task_id)[0]
            assert " attempt=1 " in interrupted
            assert f"reason={json.dumps(RESTART_ERROR, ensure_ascii=False)}" in interrupted
            report = restarted.report(task_id)
            assert [(a["attempt"], a["outcome"]) for a in report["attempts"]] == [
                (1, "interrupted"),
                (2, "succeeded"),
            ]
            assert report["attempt"] == 2
            first = json.loads(
                (service.data_dir / "tasks" / task_id / "attempts" / "1" / "report.json").read_text(
                    "utf-8"
                )
            )
            assert first["complete"] is False
            assert _status_file(restarted, task_id)["consecutive_interruptions"] == 0
        finally:
            if restarted is not None:
                stop_service(restarted)
            stop_service(service)


def test_a_failed_attempt_clears_the_consecutive_interruptions(tmp_path):
    with FakeModel() as model:
        model.inject_error("timeout", kind="dossier")  # attempt 1 hangs: interrupted
        # Only attempt 2 gets to its term list request, which fails it at once.
        model.inject_error("401", kind="term_list")
        service = launch_service(model, tmp_path)
        restarted = None
        try:
            task_id = service.submit()
            service.wait_for(task_id, lambda s: s["state"] == "running" and model.requests)

            _kill(service)
            restarted = launch_service(model, tmp_path, data_dir=service.data_dir)
            final = restarted.wait(task_id)

            assert final["state"] == "failed"
            assert final["attempt"] == 2
            outcomes = [a["outcome"] for a in restarted.report(task_id)["attempts"]]
            assert outcomes == ["interrupted", "failed"]
            assert _status_file(restarted, task_id)["consecutive_interruptions"] == 0
        finally:
            if restarted is not None:
                stop_service(restarted)
            stop_service(service)


def test_the_interrupted_task_runs_first_and_queued_tasks_keep_their_order(tmp_path):
    pauses = tmp_path / "pauses"
    pauses.mkdir()
    with FakeModel() as model:
        model.inject_error("timeout", kind="dossier")
        service = launch_service(model, tmp_path)
        restarted = None
        try:
            first = service.submit()
            second = service.submit()
            third = service.submit()
            service.wait_for(first, lambda s: s["state"] == "running" and model.requests)
            assert [service.status(t)["queue_position"] for t in (second, third)] == [1, 2]
            before = {t: _status_file(service, t) for t in (second, third)}

            _kill(service)
            restarted = launch_service(
                model, tmp_path, data_dir=service.data_dir, env={"ZT_TEST_PAUSE_AFTER_MARKER": str(pauses)}
            )

            # The interrupted task starts first; the queued ones wait in their order.
            _wait_for_file(pauses / f"{first}-2.paused")
            assert [restarted.status(t)["queue_position"] for t in (second, third)] == [1, 2]
            for task_id in (second, third):
                after = _status_file(restarted, task_id)
                assert after["state"] == "queued"
                for key in ("queue_class", "queue_key", "attempt", "consecutive_interruptions"):
                    assert after[key] == before[task_id][key], key
                assert after["consecutive_interruptions"] == 0  # queued: no interruption
                assert _events(restarted, task_id) == []
            (pauses / f"{first}-2.paused").unlink()

            for task_id in (second, third):
                _wait_for_file(pauses / f"{task_id}-1.paused", timeout=300).unlink()
            for task_id in (first, second, third):
                assert restarted.wait(task_id)["state"] == "succeeded"
            assert [restarted.status(t)["attempt"] for t in (first, second, third)] == [2, 1, 1]
        finally:
            if restarted is not None:
                stop_service(restarted)
            stop_service(service)


def test_the_third_interruption_in_a_row_fails_the_task(tmp_path):
    with FakeModel(delay=600) as model:  # no attempt ever ends by itself
        services = [launch_service(model, tmp_path)]
        try:
            task_id = services[0].submit()
            for attempt in (1, 2, 3):
                services[-1].wait_for(
                    task_id,
                    lambda s: s["state"] == "running"
                    and s["attempt"] == attempt
                    and len(model.requests_of("dossier")) >= attempt,
                )
                _kill(services[-1])
                services.append(launch_service(model, tmp_path, data_dir=services[0].data_dir))

            final = services[-1].status(task_id)
            assert final["state"] == "failed"
            assert final["error"] == MANY_RESTARTS_ERROR
            assert final["attempt"] == 3
            assert _events(services[-1], task_id) == ["中断", "失败"]
            assert _events(services[1], task_id)[:2] == ["中断", "重新排队并开始新尝试"]
            outcomes = [a["outcome"] for a in services[-1].report(task_id)["attempts"]]
            assert outcomes == ["interrupted"] * 3
            time.sleep(1)
            assert services[-1].status(task_id)["state"] == "failed"  # never requeued
        finally:
            for service in reversed(services):
                stop_service(service)


def test_calls_answered_before_the_interruption_are_not_sent_again(tmp_path):
    with FakeModel() as model:
        model.inject_error("timeout", kind="review")  # attempt 1 hangs on its first review
        service = launch_service(model, tmp_path)
        restarted = None
        try:
            task_id = service.submit()
            service.wait_for(task_id, lambda s: bool(model.requests_of("review")))
            _quiet(model)  # every other call of attempt 1 is answered and cached
            first = list(model.requests)
            answered = {r.prompt for r in first if r.fault is None}
            hung = [r.prompt for r in first if r.fault == "timeout"]

            _kill(service)
            restarted = launch_service(model, tmp_path, data_dir=service.data_dir)
            final = restarted.wait(task_id)

            assert final["state"] == "succeeded", final["error"]
            second = model.requests[len(first) :]
            assert second, "attempt 2 sent nothing"
            assert [r.prompt for r in second if r.prompt in answered] == []
            assert hung[0] in {r.prompt for r in second}
            assert {"dossier", "term_list"}.isdisjoint(r.kind for r in second)
        finally:
            if restarted is not None:
                stop_service(restarted)
            stop_service(service)


def test_a_gloss_request_that_failed_before_the_interruption_succeeds_and_changes_later_calls(
    tmp_path,
):
    with FakeModel() as model:
        model.respond("term_list", lambda body: json.dumps(MODEL_TERMS, ensure_ascii=False))
        model.respond("promoted_glosses", rewriting_responder)
        # Attempt 1: the promoted glosses request fails for good (1 + 3 sends);
        # the task goes on without the glosses, and hangs on its first review.
        model.fail_same_request(["500"] * 4, kind="promoted_glosses")
        model.inject_error("timeout", kind="review")
        service = launch_service(model, tmp_path)
        restarted = None
        try:
            task_id = service.submit()
            service.wait_for(task_id, lambda s: bool(model.requests_of("review")))
            _quiet(model)
            first = list(model.requests)
            assert [r.fault for r in first if r.kind == "promoted_glosses"] == ["500"] * 4
            first_batches = {r.prompt for r in first if r.kind == "translate_batch"}
            assert first_batches

            _kill(service)
            restarted = launch_service(model, tmp_path, data_dir=service.data_dir)
            final = restarted.wait(task_id)

            assert final["state"] == "succeeded", final["error"]
            second = model.requests[len(first) :]
            [gloss] = [r for r in second if r.kind == "promoted_glosses"]
            assert gloss.fault is None
            batches = [r.prompt for r in second if r.kind == "translate_batch"]
            # The term list changed, so every batch request changed and is sent.
            assert batches, "the changed batches were not sent again"
            assert all(p not in first_batches for p in batches)
            terms = restarted.http.get(f"/tasks/{task_id}/files/terms").json()["terms"]
            assert {t["english"][0]: t for t in terms}["harness"]["gloss_zh"] == HARNESS_GLOSS
        finally:
            if restarted is not None:
                stop_service(restarted)
            stop_service(service)


SPAWN_CHILD = {"ZT_TEST_WORKER_SPAWN_CHILD": "1"}


def _worker_and_children(service, task_id: str) -> tuple[psutil.Process, list[int]]:
    """Wait for the worker and its spawned child; return the worker and its other marked processes."""
    deadline = time.monotonic() + 120
    while True:
        marked = _marked(task_id)
        workers = [p for p in marked if p.ppid() == service.process.pid]
        others = [p.pid for p in marked if p.ppid() != service.process.pid]
        if workers and others:
            return workers[0], others
        assert time.monotonic() < deadline, "the worker never spawned its child"
        time.sleep(0.1)


def test_a_killed_workers_child_is_reaped_before_the_new_attempt_starts(tmp_path):
    pauses = tmp_path / "pauses"
    pauses.mkdir()
    with FakeModel(delay=600) as model:
        service = launch_service(model, tmp_path, env=SPAWN_CHILD)
        restarted = None
        try:
            task_id = service.submit()
            worker, children = _worker_and_children(service, task_id)
            # The worker dies first, so it cannot end its child; then the service.
            worker.kill()
            _kill(service)
            time.sleep(1)
            assert all(_alive(pid) for pid in children), "the child did not outlive its worker"

            restarted = launch_service(
                model, tmp_path, data_dir=service.data_dir, env={"ZT_TEST_PAUSE_AFTER_MARKER": str(pauses)}
            )
            _wait_for_file(pauses / f"{task_id}-2.paused")

            assert [pid for pid in children if _alive(pid)] == []
            assert _marked(task_id) == []
        finally:
            if restarted is not None:
                stop_service(restarted)
            stop_service(service)


def test_the_children_of_a_worker_killed_under_a_live_service_are_reaped(tmp_path):
    with FakeModel(delay=600) as model:
        service = launch_service(model, tmp_path, env=SPAWN_CHILD)
        try:
            task_id = service.submit()
            worker, children = _worker_and_children(service, task_id)

            worker.kill()
            final = service.wait(task_id, timeout=60)

            assert final["state"] == "failed"
            assert "论文任务进程异常退出" in final["error"]
            assert [pid for pid in children if _alive(pid)] == []
            assert _marked(task_id) == []
        finally:
            stop_service(service)


def test_a_worker_whose_service_is_gone_ends_its_children_and_then_itself(tmp_path):
    with FakeModel(delay=600) as model:
        service = launch_service(model, tmp_path, env=SPAWN_CHILD)
        try:
            task_id = service.submit()
            worker, children = _worker_and_children(service, task_id)
            assert os.getpgid(worker.pid) == worker.pid  # its own process group
            assert all(os.getpgid(pid) == worker.pid for pid in children)

            _kill(service)

            deadline = time.monotonic() + 20
            while _marked(task_id) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert _marked(task_id) == []
            assert not _alive(worker.pid)
            assert [pid for pid in children if _alive(pid)] == []
        finally:
            stop_service(service)


def _old_task(tasks: Path, name: str, state: str, **status) -> str:
    """Write a task directory as the service before M3 left it."""
    task_id = (name * 32)[:32]
    directory = tasks / task_id
    directory.mkdir(parents=True)
    shutil.copy(EXCERPT, directory / "source.pdf")
    (directory / "task.json").write_text(
        json.dumps({"task_id": task_id, "filename": f"{state}.pdf", "submitted_at": time.time()}),
        encoding="utf-8",
    )
    (directory / "status.json").write_text(
        json.dumps(
            {
                "state": state,
                "stage": status.pop("stage", "排队中"),
                "progress": status.pop("progress", 0),
                "error": status.pop("error", None),
                "kept_source_paragraphs": 0,
                "kept_source_algorithms": 0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return task_id


def test_task_directories_from_before_the_upgrade(tmp_path, fake_model, start_service):
    data_dir = tmp_path / "old-data"
    tasks = data_dir / "tasks"
    succeeded = _old_task(tasks, "a", "succeeded", stage="完成", progress=100)
    (tasks / succeeded / "mono.pdf").write_bytes(b"%PDF-old")
    failed = _old_task(tasks, "b", "failed", stage="翻译与审校", progress=40, error="旧的失败")
    running = _old_task(tasks, "c", "running", stage="翻译与审校", progress=50)
    queued = _old_task(tasks, "d", "queued")
    terminal_before = {
        t: (tasks / t / "status.json").read_bytes() for t in (succeeded, failed)
    }

    service = start_service(fake_model, data_dir=data_dir)

    for task_id, before in terminal_before.items():
        assert (tasks / task_id / "status.json").read_bytes() == before
    old_running = service.status(running)
    assert old_running["state"] == "failed"
    assert old_running["error"] == UPGRADE_ERROR
    assert old_running["attempt"] == 0
    assert _events(service, running) == ["失败"]
    final = service.wait(queued)
    assert final["state"] == "succeeded", final["error"]
    assert final["attempt"] == 1
    assert service.status(running)["state"] == "failed"  # never requeued
    assert service.status(succeeded)["state"] == "succeeded"
    assert service.status(failed)["error"] == "旧的失败"


def test_a_task_that_cannot_be_restored_fails_alone_and_the_others_are_restored(tmp_path):
    with FakeModel() as model:
        model.inject_error("timeout", kind="dossier")
        service = launch_service(model, tmp_path)
        restarted = None
        try:
            broken = service.submit()
            waiting = service.submit()
            service.wait_for(broken, lambda s: s["state"] == "running" and model.requests)
            _kill(service)
            tasks = service.data_dir / "tasks"
            # Its report.json cannot be written.
            (tasks / broken / "report.json").mkdir()
            # A status file that is not JSON at all.
            unreadable = "e" * 32
            (tasks / unreadable).mkdir()
            (tasks / unreadable / "task.json").write_text(
                json.dumps({"task_id": unreadable, "filename": "坏.pdf", "submitted_at": time.time()}),
                encoding="utf-8",
            )
            (tasks / unreadable / "status.json").write_text("{not json", encoding="utf-8")

            restarted = launch_service(model, tmp_path, data_dir=service.data_dir)

            for task_id in (broken, unreadable):
                status = restarted.status(task_id)
                assert status["state"] == "failed"
                assert "翻译服务接管任务出错" in status["error"], status["error"]
                assert _events(restarted, task_id)[-1] == "失败"
            final = restarted.wait(waiting)
            assert final["state"] == "succeeded", final["error"]
        finally:
            if restarted is not None:
                stop_service(restarted)
            stop_service(service)
