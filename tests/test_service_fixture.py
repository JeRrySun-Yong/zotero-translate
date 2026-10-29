"""The test fixtures leave no process behind (closure ticket 09).

A service a test started once ran for 3 hours after its pytest run was
interrupted, and a task subprocess once ran for 31 minutes after its test
ended (closure backlog). ``stop_service`` ends the service and its task
subprocesses, and an interrupted test run (SIGINT, SIGTERM) still stops its
services.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FakeModel

REPO = Path(__file__).resolve().parent.parent

# Run by an inner pytest: start a service whose task waits for the fake
# model, write down where it runs, and wait to be interrupted.
INNER_TEST = """
import json
import os
import time
from pathlib import Path

from tests.fake_model import FakeModel


def test_holds_a_running_task(start_service):
    with FakeModel(delay=600) as model:
        service = start_service(model)
        task_id = service.submit()
        service.wait_for(task_id, lambda s: s["state"] == "running" and model.requests)
        where = {"service": service.process.pid, "task_dir": str(service.data_dir / "tasks" / task_id)}
        out = Path(os.environ["ZT_TEST_WHERE"])
        out.with_suffix(".tmp").write_text(json.dumps(where))
        out.with_suffix(".tmp").replace(out)
        time.sleep(600)
"""


def _task_processes(task_dir: Path | str) -> list[int]:
    """The running task subprocesses of a task directory."""
    found = subprocess.run(["pgrep", "-f", str(task_dir)], capture_output=True, text=True)
    return [int(pid) for pid in found.stdout.split()]


def _running(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return state.returncode == 0 and not state.stdout.strip().startswith("Z")


def test_stopping_the_service_also_ends_its_running_task_subprocess(tmp_path):
    with FakeModel(delay=600) as model:
        service = launch_service(model, tmp_path)
        try:
            task_id = service.submit()
            service.wait_for(task_id, lambda s: s["state"] == "running" and model.requests)
            workers = _task_processes(service.data_dir / "tasks" / task_id)
            assert workers

            stop_service(service)

            assert service.process.poll() is not None
            assert [pid for pid in workers if _running(pid)] == []
        finally:
            stop_service(service)  # again, should the test end before it did


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_an_interrupted_test_run_stops_its_service_and_task_subprocess(tmp_path, sig):
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "test_inner.py").write_text(INNER_TEST, encoding="utf-8")
    where_file = tmp_path / "where.json"
    run = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests.conftest"]
        + ["--rootdir", str(inner), str(inner / "test_inner.py")],
        cwd=REPO,
        env={**os.environ, "ZT_TEST_WHERE": str(where_file)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    service, workers = None, []
    try:
        deadline = time.monotonic() + 120
        while not where_file.exists():
            assert run.poll() is None, run.stdout.read()
            assert time.monotonic() < deadline, "the inner test run never started its task"
            time.sleep(0.2)
        where = json.loads(where_file.read_text("utf-8"))
        service = where["service"]
        workers = _task_processes(where["task_dir"])
        assert workers

        run.send_signal(sig)
        output = run.communicate(timeout=60)[0]

        assert not _running(service), output
        assert [pid for pid in workers if _running(pid)] == [], output
    finally:
        if run.poll() is None:
            run.kill()
        for pid in [service, *workers]:
            if pid is not None and _running(pid):
                os.kill(pid, signal.SIGKILL)
