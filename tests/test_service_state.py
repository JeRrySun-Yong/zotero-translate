"""The service alone writes task state (M3 ticket 03; spec sections 1, 2, 3 and 5).

One service per data directory; task events in the service log; one task
report per attempt, with a summary of all attempts in ``report.json``; the
paper task subprocess (worker) reports, the service decides.
"""

import json
import socket
import subprocess
import time
from pathlib import Path

import psutil

from tests.conftest import FAKE_API_KEY
from tests.conftest import FAKE_ARGON_API_KEY
from tests.conftest import launch_service
from tests.conftest import serve_command
from tests.conftest import stop_service
from tests.conftest import write_env_file
from tests.fake_model import FakeModel

MARK_ENV = "ZT_PROCESS_MARK"


def _env_files(root: Path, model: FakeModel) -> tuple[Path, Path]:
    return (
        write_env_file(root, model.base_url, FAKE_API_KEY),
        write_env_file(root, model.base_url, FAKE_ARGON_API_KEY),
    )


def _task_files(data_dir: Path) -> dict[str, bytes]:
    tasks = data_dir / "tasks"
    return {str(p.relative_to(tasks)): p.read_bytes() for p in tasks.rglob("*") if p.is_file()}


def test_second_service_on_the_same_data_directory_exits_and_leaves_tasks_alone(tmp_path):
    with FakeModel(delay=600) as model:
        service = launch_service(model, tmp_path)
        try:
            task_id = service.submit()
            service.submit()
            service.wait_for(task_id, lambda s: s["state"] == "running" and model.requests)
            before = _task_files(service.data_dir)

            second = subprocess.run(
                serve_command(*_env_files(tmp_path, model), service.data_dir),
                capture_output=True,
                text=True,
                timeout=60,
            )

            assert second.returncode != 0
            assert "已有翻译服务在使用数据目录" in second.stderr, second.stderr
            assert str(service.data_dir) in second.stderr
            assert second.stdout == ""  # never announced an address
            assert _task_files(service.data_dir) == before
            assert service.status(task_id)["state"] == "running"
        finally:
            stop_service(service)


def test_service_exits_when_its_port_is_taken_and_leaves_tasks_alone(tmp_path):
    with FakeModel() as model:
        service = launch_service(model, tmp_path)
        task_id = service.submit()
        service.wait(task_id)
        stop_service(service)
        before = _task_files(service.data_dir)
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]

            second = subprocess.run(
                serve_command(*_env_files(tmp_path, model), service.data_dir, port=port),
                capture_output=True,
                text=True,
                timeout=60,
            )

        assert second.returncode != 0
        assert f"端口 {port} 已被占用" in second.stderr, second.stderr
        assert second.stdout == ""
        assert _task_files(service.data_dir) == before


def _event(line: str) -> str:
    return line.split(" event=", 1)[1].split(" ", 1)[0]


def test_service_log_has_a_line_per_task_event_without_api_keys(fake_model, start_service):
    fake_model.inject_error("401", kind="dossier")
    service = start_service(fake_model)

    bad = service.submit(filename="bad.pdf")  # its dossier request gets the 401
    failed = service.wait(bad)
    assert failed["state"] == "failed"
    good = service.submit(filename="好论文.pdf")
    assert service.wait(good)["state"] == "succeeded"

    # The log line is written when the transition is saved, before any poll sees it.
    good_lines = service.log_lines(good)
    assert [_event(line) for line in good_lines] == ["提交", "开始", "成功"]
    assert all('file="好论文.pdf"' in line for line in good_lines)
    assert [line.split(" attempt=", 1)[1].split(" ", 1)[0] for line in good_lines] == ["0", "1", "1"]
    bad_lines = service.log_lines(bad)
    assert [_event(line) for line in bad_lines] == ["提交", "开始", "失败"]
    assert f"reason={json.dumps(failed['error'], ensure_ascii=False)}" in bad_lines[-1]
    for line in service.output:
        assert FAKE_API_KEY not in line and FAKE_ARGON_API_KEY not in line
    assert not any("GET /tasks" in line for line in service.output)


def test_each_attempt_has_a_report_and_report_json_summarises_the_attempts(
    fake_model, start_service
):
    service = start_service(fake_model)

    task_id = service.submit()
    final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    task_dir = service.data_dir / "tasks" / task_id
    attempt_report = json.loads((task_dir / "attempts" / "1" / "report.json").read_text("utf-8"))
    assert attempt_report["attempt"] == 1
    assert attempt_report["state"] == "succeeded"
    report = service.report(task_id)
    assert report["attempt"] == 1
    assert report["state"] == "succeeded"
    assert report["model"] == attempt_report["model"]
    [summary] = report["attempts"]
    assert summary["attempt"] == 1
    assert summary["outcome"] == "succeeded"
    assert summary["reason"] is None
    assert summary["started_at"] <= summary["ended_at"] <= time.time()


def test_new_task_state_file_starts_with_the_initial_counters(fake_model, start_service, tmp_path):
    pauses = tmp_path / "pauses"
    pauses.mkdir()
    service = start_service(fake_model, env={"ZT_TEST_PAUSE_AFTER_MARKER": str(pauses)})

    first = service.submit()  # held at the pause, so the second stays queued
    second = service.submit()

    status = json.loads((service.data_dir / "tasks" / second / "status.json").read_text("utf-8"))
    assert status["state"] == "queued"
    assert {
        key: status[key]
        for key in (
            "attempt",
            "reruns_used",
            "consecutive_interruptions",
            "next_attempt_at",
            "queue_class",
            "error",
            "failure_category",
            "cancel_requested",
            "process_marker",
            "cleanup_error",
        )
    } == {
        "attempt": 0,
        "reruns_used": 0,
        "consecutive_interruptions": 0,
        "next_attempt_at": None,
        "queue_class": "first",
        "error": None,
        "failure_category": None,
        "cancel_requested": False,
        "process_marker": None,
        "cleanup_error": None,
    }
    submitted_at = json.loads(
        (service.data_dir / "tasks" / second / "task.json").read_text("utf-8")
    )["submitted_at"]
    assert status["queue_key"] == submitted_at
    _wait_for_file(pauses / f"{first}-1.paused").unlink()


def _wait_for_file(path: Path, timeout: float = 60) -> Path:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.05)
    return path


def _marked_processes(task_id: str) -> list[psutil.Process]:
    """Running processes whose process marker names ``task_id``."""
    found = []
    for process in psutil.process_iter():
        try:
            marked = process.environ().get(MARK_ENV, "").startswith(f"{task_id}:")
            if marked and process.status() != psutil.STATUS_ZOMBIE:
                found.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return found


def test_process_marker_is_saved_before_the_worker_starts_and_passed_to_it(tmp_path):
    pauses = tmp_path / "pauses"
    pauses.mkdir()
    with FakeModel(delay=600) as model:
        service = launch_service(model, tmp_path, env={"ZT_TEST_PAUSE_AFTER_MARKER": str(pauses)})
        try:
            task_id = service.submit()
            flag = _wait_for_file(pauses / f"{task_id}-1.paused")

            status = json.loads(
                (service.data_dir / "tasks" / task_id / "status.json").read_text("utf-8")
            )
            assert status["state"] == "running"
            assert status["attempt"] == 1
            assert status["process_marker"] == f"{task_id}:1"
            assert _marked_processes(task_id) == []  # no worker yet

            flag.unlink()
            service.wait_for(task_id, lambda s: bool(model.requests))
            [worker] = _marked_processes(task_id)
            assert worker.environ()[MARK_ENV] == f"{task_id}:1"
            assert worker.ppid() == service.process.pid
        finally:
            stop_service(service)


def test_worker_does_not_write_state_and_a_restart_still_fails_the_task(tmp_path):
    pauses = tmp_path / "pauses"
    pauses.mkdir()
    with FakeModel() as model:
        service = launch_service(model, tmp_path, env={"ZT_TEST_PAUSE_BEFORE_ADOPT": str(pauses)})
        restarted = None
        try:
            task_id = service.submit()
            _wait_for_file(pauses / f"{task_id}-1.paused", timeout=300)

            # The worker has succeeded and exited; only the service decides the state.
            assert _marked_processes(task_id) == []
            assert (service.data_dir / "tasks" / task_id / "mono.pdf").is_file()
            held = service.status(task_id)
            assert held["state"] == "running"
            assert held["progress"] < 100

            # The service dies holding the result: attempt 1 ends as interrupted,
            # and its success is never adopted.
            service.process.kill()
            service.process.wait(timeout=10)
            restarted = launch_service(model, tmp_path, data_dir=service.data_dir)
            time.sleep(1)
            final = restarted.status(task_id)

            assert final["state"] == "failed"
            assert final["error"] == "翻译服务重启，任务中断"
            report = restarted.report(task_id)
            [summary] = report["attempts"]
            assert summary["outcome"] == "interrupted"
            assert summary["reason"] == "翻译服务重启，任务中断"
            assert report["complete"] is False
            assert report["incomplete_reason"] == "翻译服务重启，任务中断"
            assert [_event(line) for line in restarted.log_lines(task_id)] == ["失败"]
        finally:
            if restarted is not None:
                stop_service(restarted)
            stop_service(service)


def test_report_json_gives_the_failure_when_the_worker_cannot_read_its_settings(tmp_path):
    with FakeModel() as model:
        env_file, argon_env_file = _env_files(tmp_path, model)
        service = launch_service(model, tmp_path, env_file=env_file, argon_env_file=argon_env_file)
        try:
            env_file.unlink()  # the service checked it at startup; the worker cannot read it

            task_id = service.submit()
            final = service.wait(task_id)

            assert final["state"] == "failed"
            assert final["error"]
            report = service.report(task_id)
            assert report["state"] == "failed"
            assert report["error"] == final["error"]
            assert report["attempts"][0]["outcome"] == "failed"
        finally:
            stop_service(service)


def test_a_task_the_service_cannot_finish_fails_and_the_next_task_still_runs(tmp_path):
    pauses = tmp_path / "pauses"
    pauses.mkdir()
    with FakeModel() as model:
        service = launch_service(model, tmp_path, env={"ZT_TEST_PAUSE_BEFORE_ADOPT": str(pauses)})
        try:
            broken = service.submit()
            flag = _wait_for_file(pauses / f"{broken}-1.paused", timeout=300)
            # The service cannot write this task's report.json.
            (service.data_dir / "tasks" / broken / "report.json").mkdir()
            flag.unlink()

            failed = service.wait(broken)
            assert failed["state"] == "failed"
            assert "翻译服务处理任务出错" in failed["error"]

            nxt = service.submit()
            _wait_for_file(pauses / f"{nxt}-1.paused", timeout=300).unlink()
            assert service.wait(nxt)["state"] == "succeeded"
        finally:
            stop_service(service)


def test_malformed_worker_reports_are_dropped_and_logged_without_failing_the_attempt(tmp_path):
    with FakeModel() as model:
        # Test only: the worker also sends a line that is not JSON and a
        # progress report without a stage.
        service = launch_service(model, tmp_path, env={"ZT_TEST_WORKER_BAD_REPORTS": "1"})
        try:
            task_id = service.submit()
            final = service.wait(task_id)

            assert final["state"] == "succeeded", final["error"]
            events = [_event(line) for line in service.log_lines(task_id)]
            assert events.count("丢弃上报") == 2
            assert events[-1] == "成功"
        finally:
            stop_service(service)
