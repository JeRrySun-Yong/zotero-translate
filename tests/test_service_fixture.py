"""The test fixtures leave no process behind (closure ticket 09).

A service a test started once ran for 3 hours after its pytest run was
interrupted, and a task subprocess once ran for 31 minutes after its test
ended (closure backlog). ``stop_service`` ends the service and its task
subprocesses, and an interrupted test run (SIGINT, SIGTERM) still stops its
services.
"""

import errno
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FakeModel

REPO = Path(__file__).resolve().parent.parent

# Run by an inner pytest: start a service whose task waits for the fake
# model, write down where it runs, and wait to be interrupted.
INNER_TEST = """
import faulthandler
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
        # Still alive 30 s after being told where it runs (the interrupt comes
        # within seconds): print every thread's stack, to see where it hangs.
        faulthandler.dump_traceback_later(30, exit=False)
        time.sleep(600)
"""

# A process group like the service's: the leader dies at once on SIGTERM; its
# member, like a task subprocess on a heavily loaded machine, takes 2 seconds
# to exit.
SLOW_TO_EXIT = """
import signal, sys, time
signal.signal(signal.SIGTERM, lambda *_: (time.sleep(2), sys.exit(0)))
print("ready", flush=True)
time.sleep(600)
"""
LEADER = """
import subprocess, sys
member = subprocess.Popen([sys.executable, "-c", sys.argv[1]], stdout=subprocess.PIPE, text=True)
member.stdout.readline()
print(member.pid, flush=True)
member.wait()
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


def test_stopping_the_service_waits_for_a_task_subprocess_that_is_still_exiting(monkeypatch):
    # macOS answers killpg(group, 0) with EPERM, as for a group of zombies, as
    # soon as the group's last live process has begun to exit, while ps still
    # shows it (state "?E"); under heavy load a task subprocess stays so long
    # enough for a test to see it running (closure ticket 09, follow-up). The
    # answer is simulated here for the member's 2-second exit.
    leader = subprocess.Popen(
        [sys.executable, "-c", LEADER, SLOW_TO_EXIT],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    member = int(leader.stdout.readline())
    killpg = os.killpg

    def killpg_as_while_exiting(group: int, sig: int) -> None:
        if group == leader.pid and sig == 0:
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))
        killpg(group, sig)

    monkeypatch.setattr(os, "killpg", killpg_as_while_exiting)
    try:
        stop_service(SimpleNamespace(process=leader))

        assert leader.poll() is not None
        assert not _running(member)
    finally:
        for pid in (leader.pid, member):
            if _running(pid):
                os.kill(pid, signal.SIGKILL)
        leader.stdout.close()


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
        try:
            output = run.communicate(timeout=60)[0]
        except subprocess.TimeoutExpired:
            run.kill()
            try:
                output = run.communicate(timeout=10)[0]
            except subprocess.TimeoutExpired as e:  # its service still holds the pipe
                output = (e.output or b"").decode(errors="replace") if isinstance(
                    e.output, bytes
                ) else (e.output or "")
            raise AssertionError(
                f"the inner test run did not exit after the interrupt "
                f"(exit status {run.poll()}):\n{output}"
            ) from None

        assert not _running(service), output
        assert [pid for pid in workers if _running(pid)] == [], output
    finally:
        if run.poll() is None:
            run.kill()
        for pid in [service, *workers]:
            if pid is not None and _running(pid):
                os.kill(pid, signal.SIGKILL)
