"""Automatic rerun (M3 ticket 05; spec sections 1, 4 and 5, seam 1 "自动重跑").

An attempt that fails for a retryable reason is run again by the service, at
most twice, after a wait of 60 and then 300 seconds (shortened here with the
test-only setting). A credential error (HTTP 401, 403) fails the task at
once, also when it is one of several failed batches. While a task waits, the
next task runs; a due rerun goes ahead of first attempts without preempting
the running one. Due times survive a restart, and a rerun that was queued, or
whose attempt was interrupted, is not counted again.

Only the HTTP interface, the service log, and the status and report files are
looked at.
"""

import json
import math
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import FAKE_API_KEY
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.conftest import write_env_file
from tests.fake_model import FAKE_DOSSIER
from tests.fake_model import FakeModel

WAITS = "ZT_TEST_RERUN_WAITS_SECONDS"
PAUSE_AFTER_MARKER = "ZT_TEST_PAUSE_AFTER_MARKER"
PAUSE_BEFORE_RERUN_QUEUE = "ZT_TEST_PAUSE_BEFORE_RERUN_QUEUE"
PAUSE_BEFORE_REAP = "ZT_TEST_PAUSE_BEFORE_REAP"


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


def _status_path(service, task_id: str) -> Path:
    return service.data_dir / "tasks" / task_id / "status.json"


def _status_file(service, task_id: str) -> dict:
    return json.loads(_status_path(service, task_id).read_text("utf-8"))


def _kill(service) -> None:
    """Kill the service with SIGKILL, as a crash would."""
    service.process.kill()
    service.process.wait(timeout=10)


def _fail_dossier(model: FakeModel) -> None:
    """Every dossier answer is rejected: an attempt fails, retryable, after 4 sends (about 7 s)."""
    model.respond("dossier", lambda body: "not a dossier")


def _answer_dossier(model: FakeModel) -> None:
    model.respond("dossier", lambda body: json.dumps(FAKE_DOSSIER, ensure_ascii=False))


def _pauses(tmp_path: Path, name: str = "pauses") -> Path:
    directory = tmp_path / name
    directory.mkdir()
    return directory


@pytest.mark.parametrize("code", ["401", "403"])
def test_a_credential_error_fails_the_task_at_once_without_a_rerun(
    fake_model, start_service, code
):
    fake_model.inject_error(code, kind="dossier")
    service = start_service(fake_model, env={WAITS: "0.5,0.5"})

    task_id = service.submit()
    final = service.wait(task_id)

    assert final["state"] == "failed"
    assert f"HTTP {code}" in final["error"], final["error"]
    assert final["attempt"] == 1
    assert final["reruns_used"] == 0
    assert final["reruns_max"] == 2
    assert final["next_attempt_in_seconds"] is None
    assert final["queue_position"] is None
    time.sleep(2)  # past both waits: nothing is rerun
    assert service.status(task_id)["state"] == "failed"
    assert len(fake_model.requests_of("dossier")) == 1
    assert _events(service, task_id) == ["提交", "开始", "失败"]
    assert _status_file(service, task_id)["failure_category"] == "permanent"
    assert service.report(task_id)["failure_category"] == "permanent"


@pytest.mark.parametrize("unwritable", ["attempt report", "task report"])
def test_a_credential_error_stays_permanent_when_a_report_cannot_be_written(
    tmp_path, fake_model, start_service, unwritable
):
    # The worker's attempt report, or the service's task report, cannot be
    # written: the 401 must still fail the task at once, without a rerun.
    pauses = _pauses(tmp_path)
    fake_model.inject_error("401", kind="dossier")
    pause = PAUSE_AFTER_MARKER if unwritable == "attempt report" else "ZT_TEST_PAUSE_BEFORE_ADOPT"
    service = start_service(fake_model, env={WAITS: "0.5,0.5", pause: str(pauses)})

    task_id = service.submit()
    flag = _wait_for_file(pauses / f"{task_id}-1.paused")
    task_dir = service.data_dir / "tasks" / task_id
    if unwritable == "attempt report":
        (task_dir / "attempts" / "1" / "report.json").mkdir(parents=True)
    else:
        (task_dir / "report.json").mkdir()
    flag.unlink()
    final = service.wait(task_id)
    time.sleep(1.5)  # past both waits: nothing is rerun

    assert final["state"] == "failed"
    assert service.status(task_id)["state"] == "failed"
    assert service.status(task_id)["reruns_used"] == 0
    assert _status_file(service, task_id)["failure_category"] == "permanent"
    assert _events(service, task_id) == ["提交", "开始", "失败"]


@pytest.mark.parametrize("breakage", ["not UTF-8", "unreadable", "directory unreadable"])
def test_settings_the_worker_cannot_read_fail_the_task_at_once_without_a_rerun(tmp_path, breakage):
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    with FakeModel() as model:
        env_file = write_env_file(env_dir, model.base_url, FAKE_API_KEY)
        service = launch_service(model, tmp_path, env_file=env_file, env={WAITS: "0.5,0.5"})
        try:
            # The service checked the file at startup; the worker cannot read it.
            if breakage == "not UTF-8":
                env_file.write_bytes(b"OPENAI_BASE_URL=\xff\xfe\n")
            elif breakage == "unreadable":
                env_file.chmod(0)
            else:
                env_dir.chmod(0)
            task_id = service.submit()
            final = service.wait(task_id)
            time.sleep(1.5)  # past both waits

            assert final["state"] == "failed"
            assert service.status(task_id)["state"] == "failed"
            assert service.status(task_id)["reruns_used"] == 0
            assert _status_file(service, task_id)["failure_category"] == "permanent"
            assert _events(service, task_id) == ["提交", "开始", "失败"]
        finally:
            env_dir.chmod(0o700)
            env_file.chmod(0o600)
            stop_service(service)


def test_several_failed_batches_with_one_401_fail_the_task_as_permanent(
    fake_model, start_service
):
    # The first batch fails retryably 4 times; the next batch gets a 401.
    fake_model.fail_same_request(["500"] * 4, kind="translate_batch")
    fake_model.inject_error("401", kind="translate_batch")
    service = start_service(fake_model, env={WAITS: "0.5,0.5"})

    task_id = service.submit()
    final = service.wait(task_id)

    assert final["state"] == "failed"
    assert final["attempt"] == 1, "a credential error was rerun"
    assert "HTTP 401" in final["error"], final["error"]
    report = service.report(task_id)
    assert report["failure_category"] == "permanent"
    categories = [batch["category"] for batch in report["failed_batches"]]
    assert sorted(set(categories)) == ["permanent", "retryable"], report["failed_batches"]
    assert _status_file(service, task_id)["failure_category"] == "permanent"


def test_a_retryable_failure_is_rerun_at_most_twice_and_the_last_reason_stands(
    fake_model, start_service
):
    # Each attempt's dossier request fails 4 times, each attempt differently.
    fake_model.fail_same_request(["500"] * 4 + ["429"] * 4 + ["400"] * 4, kind="dossier")
    service = start_service(fake_model, env={WAITS: "2,3"})

    task_id = service.submit()
    polls: list[dict] = []
    final = service.wait(task_id, on_poll=polls.append)

    assert final["state"] == "failed"
    assert "HTTP 400" in final["error"], final["error"]
    assert final["attempt"] == 3
    assert final["reruns_used"] == 2
    assert final["next_attempt_in_seconds"] is None
    waiting = [p for p in polls if p["state"] == "waiting_rerun"]
    first = [p for p in waiting if p["reruns_used"] == 0]
    second = [p for p in waiting if p["reruns_used"] == 1]
    assert first and second, [p["state"] for p in polls]
    for poll, wait in ((first[0], 2), (second[0], 3)):
        assert 0 <= poll["next_attempt_in_seconds"] <= wait, poll
        assert isinstance(poll["next_attempt_in_seconds"], int)
        assert poll["queue_position"] is None
        assert poll["reruns_max"] == 2
    assert "HTTP 500" in first[0]["error"], first[0]["error"]
    assert "HTTP 429" in second[0]["error"], second[0]["error"]
    for poll in polls:
        if poll["state"] in ("queued", "running"):
            assert poll["error"] is None, poll
            assert poll["next_attempt_in_seconds"] is None, poll
    assert _events(service, task_id) == [
        "提交",
        "开始",
        "等待自动重跑",
        "自动重跑开始",
        "开始",
        "等待自动重跑",
        "自动重跑开始",
        "开始",
        "失败",
    ]
    waited = [line for line in service.log_lines(task_id) if "event=等待自动重跑" in line]
    assert "HTTP 500" in waited[0] and "HTTP 429" in waited[1], waited
    report = service.report(task_id)
    assert [a["outcome"] for a in report["attempts"]] == ["failed"] * 3
    assert report["error"] == final["error"]
    assert len(fake_model.requests_of("dossier")) == 12


def test_while_a_task_waits_the_next_runs_and_the_due_rerun_goes_next_without_preempting(
    tmp_path, fake_model, start_service
):
    pauses = _pauses(tmp_path)
    _fail_dossier(fake_model)
    service = start_service(fake_model, env={WAITS: "1,600", PAUSE_AFTER_MARKER: str(pauses)})

    a, b, c = service.submit(), service.submit(), service.submit()
    _wait_for_file(pauses / f"{a}-1.paused").unlink()
    paused_b = _wait_for_file(pauses / f"{b}-1.paused")  # a failed: b runs meanwhile
    _answer_dossier(fake_model)
    queued = service.wait_for(a, lambda s: s["state"] == "queued", timeout=30)

    assert queued["reruns_used"] == 1
    assert queued["queue_position"] == 1
    assert service.status(c)["queue_position"] == 2
    assert service.status(b)["state"] == "running"  # not preempted
    paused_b.unlink()
    paused_a = _wait_for_file(pauses / f"{a}-2.paused", timeout=300)
    assert service.status(b)["state"] == "succeeded"
    assert service.status(c)["state"] == "queued"
    assert not (pauses / f"{c}-1.paused").exists()
    paused_a.unlink()
    _wait_for_file(pauses / f"{c}-1.paused", timeout=300).unlink()

    for task_id in (a, b, c):
        assert service.wait(task_id)["state"] == "succeeded"
    assert [service.status(t)["attempt"] for t in (a, b, c)] == [2, 1, 1]
    lines = service.output
    rerun_started = next(i for i, line in enumerate(lines) if f"task={a} " in line and "event=自动重跑开始" in line)
    b_succeeded = next(i for i, line in enumerate(lines) if f"task={b} " in line and "event=成功" in line)
    assert rerun_started < b_succeeded  # queued while b ran


def test_a_due_rerun_not_yet_queued_still_goes_ahead_of_first_attempts(
    tmp_path, fake_model, start_service
):
    # The rerun timer is held between "due" and "queued"; the queue must not
    # start a first attempt meanwhile. After a restart an overdue rerun is in
    # the same place until the timer queues it.
    markers = _pauses(tmp_path)
    due = _pauses(tmp_path, "due")
    _fail_dossier(fake_model)
    service = start_service(
        fake_model,
        env={WAITS: "1,600", PAUSE_AFTER_MARKER: str(markers), PAUSE_BEFORE_RERUN_QUEUE: str(due)},
    )

    a, b, c = service.submit(), service.submit(), service.submit()
    _wait_for_file(markers / f"{a}-1.paused").unlink()
    paused_b = _wait_for_file(markers / f"{b}-1.paused")  # a failed: b runs meanwhile
    _answer_dossier(fake_model)
    paused_due = _wait_for_file(due / f"{a}-1.paused", timeout=30)
    paused_b.unlink()
    service.wait_for(b, lambda s: s["state"] == "succeeded", timeout=120)
    time.sleep(1.5)

    assert not (markers / f"{c}-1.paused").exists()
    assert service.status(c)["state"] == "queued"
    paused_due.unlink()
    _wait_for_file(markers / f"{a}-2.paused", timeout=60).unlink()
    assert not (markers / f"{c}-1.paused").exists()
    _wait_for_file(markers / f"{c}-1.paused", timeout=300).unlink()

    for task_id in (a, b, c):
        assert service.wait(task_id)["state"] == "succeeded"
    assert [service.status(t)["attempt"] for t in (a, b, c)] == [2, 1, 1]


def test_a_rerun_due_while_the_next_task_is_being_reaped_still_goes_first(
    tmp_path, fake_model, start_service
):
    # The runner has picked b and is reaping b's earlier processes when a's
    # rerun comes due: b is still queued, keeps a queue position, and a goes first.
    reaps = _pauses(tmp_path, "reaps")
    _fail_dossier(fake_model)
    service = start_service(fake_model, env={WAITS: "1,600", PAUSE_BEFORE_REAP: str(reaps)})

    a, b = service.submit(), service.submit()
    _wait_for_file(reaps / f"{a}-1.paused").unlink()
    paused_b = _wait_for_file(reaps / f"{b}-1.paused", timeout=60)  # a failed and waits
    held = service.status(b)
    assert held["state"] == "queued"
    assert held["queue_position"] == 1
    _answer_dossier(fake_model)
    service.wait_for(a, lambda s: s["state"] == "queued", timeout=30)
    assert service.status(a)["queue_position"] == 1
    assert service.status(b)["queue_position"] == 2

    paused_b.unlink()
    _wait_for_file(reaps / f"{a}-2.paused", timeout=60).unlink()
    _wait_for_file(reaps / f"{b}-1.paused", timeout=300).unlink()
    for task_id in (a, b):
        assert service.wait(task_id)["state"] == "succeeded"
    assert service.status(a)["attempt"] == 2
    lines = service.output
    started = [line.split("task=", 1)[1].split(" ", 1)[0] for line in lines if "event=开始" in line]
    assert started == [a, a, b]


def test_a_restart_while_waiting_keeps_the_due_time(tmp_path):
    pauses = _pauses(tmp_path)
    with FakeModel() as model:
        _fail_dossier(model)
        service = launch_service(model, tmp_path, env={WAITS: "10,600"})
        restarted = None
        try:
            task_id = service.submit()
            service.wait_for(task_id, lambda s: s["state"] == "waiting_rerun", timeout=60)
            due = _status_file(service, task_id)["next_attempt_at"]

            _kill(service)
            _answer_dossier(model)
            restarted = launch_service(
                model,
                tmp_path,
                data_dir=service.data_dir,
                env={WAITS: "10,600", PAUSE_AFTER_MARKER: str(pauses)},
            )
            waiting = restarted.status(task_id)

            assert waiting["state"] == "waiting_rerun"
            assert waiting["reruns_used"] == 0
            assert 0 <= waiting["next_attempt_in_seconds"] <= math.ceil(due - time.time()) + 1
            assert _status_file(restarted, task_id)["next_attempt_at"] == due
            flag = _wait_for_file(pauses / f"{task_id}-2.paused", timeout=60)
            assert time.time() >= due  # never before its due time
            flag.unlink()
            final = restarted.wait(task_id)
            assert final["state"] == "succeeded", final["error"]
            assert final["attempt"] == 2
            assert final["reruns_used"] == 1
            assert _events(restarted, task_id) == ["自动重跑开始", "开始", "成功"]
        finally:
            if restarted is not None:
                stop_service(restarted)
            stop_service(service)


def test_tasks_due_together_run_by_due_time_then_by_submission(tmp_path):
    with FakeModel() as model:
        _fail_dossier(model)
        service = launch_service(model, tmp_path, env={WAITS: "600,600"})
        restarted = None
        try:
            t1, t2, t3 = service.submit(), service.submit(), service.submit()
            for task_id in (t1, t2, t3):
                service.wait_for(task_id, lambda s: s["state"] == "waiting_rerun", timeout=120)
            _kill(service)
            # All due by now: t2 and t3 at the same time, before t1.
            now = time.time()
            for task_id, due in ((t1, now - 30), (t2, now - 60), (t3, now - 60)):
                path = _status_path(service, task_id)
                status = json.loads(path.read_text("utf-8"))
                status["next_attempt_at"] = due
                path.write_text(json.dumps(status), encoding="utf-8")

            restarted = launch_service(model, tmp_path, data_dir=service.data_dir, env={WAITS: "600,600"})
            for task_id in (t1, t2, t3):
                restarted.wait_for(
                    task_id,
                    lambda s: s["state"] == "waiting_rerun" and s["reruns_used"] == 1,
                    timeout=120,
                )

            started = [
                line.split("task=", 1)[1].split(" ", 1)[0]
                for line in restarted.output
                if "event=开始" in line
            ]
            assert started == [t2, t3, t1]
        finally:
            if restarted is not None:
                stop_service(restarted)
            stop_service(service)


def test_a_queued_rerun_or_its_interrupted_attempt_is_not_counted_again_after_a_restart(tmp_path):
    pauses = _pauses(tmp_path)
    env = {WAITS: "1,600", PAUSE_AFTER_MARKER: str(pauses)}
    with FakeModel() as model:
        _fail_dossier(model)
        services = [launch_service(model, tmp_path, env=env)]
        try:
            a, b = services[0].submit(), services[0].submit()
            _wait_for_file(pauses / f"{a}-1.paused").unlink()
            _wait_for_file(pauses / f"{b}-1.paused")  # b holds the queue; a fails and waits
            services[0].wait_for(a, lambda s: s["state"] == "queued", timeout=30)
            assert _status_file(services[0], a)["reruns_used"] == 1

            # Restart with the rerun queued: b (interrupted) first, then a's rerun.
            _kill(services[0])
            _answer_dossier(model)
            services.append(launch_service(model, tmp_path, data_dir=services[0].data_dir, env=env))
            _wait_for_file(pauses / f"{b}-2.paused").unlink()
            _wait_for_file(pauses / f"{a}-2.paused", timeout=300)
            assert services[1].status(a)["reruns_used"] == 1

            # Restart during the rerun's attempt: requeued as interrupted, no rerun used.
            _kill(services[1])
            services.append(launch_service(model, tmp_path, data_dir=services[0].data_dir, env=env))
            _wait_for_file(pauses / f"{a}-3.paused").unlink()
            final = services[2].wait(a)

            assert final["state"] == "succeeded", final["error"]
            assert final["attempt"] == 3
            assert final["reruns_used"] == 1
            assert _events(services[2], a) == ["中断", "重新排队并开始新尝试", "开始", "成功"]
            assert "event=自动重跑开始" not in "\n".join(services[1].log_lines(a))
            assert services[2].status(b)["state"] == "succeeded"
        finally:
            for service in reversed(services):
                stop_service(service)


def test_a_restart_at_the_due_moment_queues_the_rerun_once(tmp_path):
    pauses = _pauses(tmp_path)
    with FakeModel() as model:
        _fail_dossier(model)
        service = launch_service(
            model, tmp_path, env={WAITS: "0.5,600", PAUSE_BEFORE_RERUN_QUEUE: str(pauses)}
        )
        restarted = None
        try:
            task_id = service.submit()
            _wait_for_file(pauses / f"{task_id}-1.paused", timeout=60)  # due, not yet queued
            due = service.status(task_id)
            assert due["state"] == "waiting_rerun"
            assert due["reruns_used"] == 0
            assert due["next_attempt_in_seconds"] == 0

            _kill(service)
            _answer_dossier(model)
            restarted = launch_service(model, tmp_path, data_dir=service.data_dir, env={WAITS: "0.5,600"})
            final = restarted.wait(task_id)

            assert final["state"] == "succeeded", final["error"]
            assert final["attempt"] == 2
            assert final["reruns_used"] == 1
            assert _events(restarted, task_id) == ["自动重跑开始", "开始", "成功"]
        finally:
            if restarted is not None:
                stop_service(restarted)
            stop_service(service)


def test_a_rerun_scheduled_again_while_the_last_one_is_being_queued_is_kept(tmp_path):
    # In process, because the window is too small to hit through HTTP: the
    # rerun timer has queued a task, and before the timer finishes, that
    # rerun's attempt fails and schedules the next rerun. The timer must not
    # drop the new schedule, or the task would wait forever.
    from zotero_translate.service import TaskQueue

    queue = TaskQueue(tmp_path / "data", tmp_path / "env", tmp_path / "argon.env", 1)

    def queue_rerun(task_id: str) -> None:
        queue._schedule_rerun(task_id, time.time() + 600, 0.0)  # failed again meanwhile

    queue._queue_rerun = queue_rerun
    queue._schedule_rerun("t", time.time() - 1, 0.0)
    threading.Thread(target=queue._rerun_forever, daemon=True).start()

    deadline = time.monotonic() + 5
    while not (queue._due.get("t", (0,))[0] > time.time() + 500) and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.5)
    with queue._cond:
        assert "t" in queue._due
        assert queue._due["t"][0] > time.time() + 500
