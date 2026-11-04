"""Shared pytest fixtures: the fake pipeline model and a running translation service.

The only test seam is the translation service HTTP interface. Each service
fixture starts the service as a subprocess on a random port, with a
temporary data directory and two environment files: one points flash (the
translation endpoint) at the fake model, the other points argon (the paper
dossier and term list endpoint, ADR 0004) at a second fake model or, when
the test gives none, at the same one.

No process outlives the tests (closure ticket 09): the service runs in its
own process group, and ``stop_service`` ends the whole group, then every
process with a process marker of one of its tasks (task subprocesses run in
groups of their own, M3 ticket 04). pytest tears the fixtures down when it
is interrupted with SIGINT, and SIGTERM is made to interrupt it the same way.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import psutil
import pytest

from tests.fake_model import FakeModel

FIXTURES = Path(__file__).parent / "fixtures"
EXCERPT = FIXTURES / "autodesign-p1.pdf"
FAKE_MODEL_NAME = "fake-gemini-for-tests"
FAKE_API_KEY = "sk-fake-test-key-0123456789"
FAKE_ARGON_API_KEY = "sk-fake-argon-key-9876543210"
ARGON_MODEL_NAME = "gemini-4-argon-high"  # the default, since the argon file names no model
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
# The environment variable that carries a task process's process marker
# "<task_id>:<attempt>" (spec section 2).
MARK_ENV = "ZT_PROCESS_MARK"


class ServiceClient:
    """Talks to a running translation service over HTTP, like the plugin does."""

    def __init__(
        self,
        base_url: str,
        process: subprocess.Popen,
        data_dir: Path,
        output: list[str] | None = None,
    ):
        self.base_url = base_url
        self.process = process
        self.data_dir = data_dir
        # Every line the service has written to its standard output so far,
        # the ``{"url": ...}`` line first; filled by a reader thread.
        self.output = output if output is not None else []
        self.http = httpx.Client(base_url=base_url, timeout=30)

    def log_lines(self, task_id: str) -> list[str]:
        """The service log lines (task events) about one task."""
        return [line for line in self.output if f"task={task_id} " in line]

    def submit(self, pdf: Path = EXCERPT, filename: str | None = None) -> str:
        response = self.http.post(
            "/tasks",
            files={"file": (filename or pdf.name, pdf.read_bytes(), "application/pdf")},
            data={"filename": filename or pdf.name},
        )
        assert response.status_code == 201, response.text
        return response.json()["task_id"]

    def status(self, task_id: str) -> dict:
        response = self.http.get(f"/tasks/{task_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def wait(self, task_id: str, timeout: float = 300, on_poll=None) -> dict:
        """Poll until the task ends; ``on_poll(status)`` sees every poll."""

        def finished(status: dict) -> bool:
            if on_poll:
                on_poll(status)
            return status["state"] in TERMINAL_STATES

        return self.wait_for(task_id, finished, timeout)

    def wait_first_failure(self, task_id: str, timeout: float = 300) -> dict:
        """Poll until the task's attempt has failed: it waits for a rerun, or failed for good."""
        return self.wait_for(task_id, lambda s: s["state"] in ("waiting_rerun", "failed"), timeout)

    def wait_for(self, task_id: str, condition, timeout: float = 300) -> dict:
        """Poll until ``condition(status)`` holds; return that status."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status(task_id)
            if condition(status):
                return status
            time.sleep(0.2)
        raise AssertionError(f"task {task_id}: condition not met in {timeout}s")

    def report(self, task_id: str) -> dict:
        """The task report the paper task left in its task directory."""
        return json.loads((self.data_dir / "tasks" / task_id / "report.json").read_text("utf-8"))

    def download(self, task_id: str, name: str) -> bytes:
        response = self.http.get(f"/tasks/{task_id}/files/{name}")
        assert response.status_code == 200, response.text
        return response.content


def wait_until(condition, timeout: float = 5.0) -> float:
    """Poll ``condition()`` until it holds; return the seconds it took, fail after ``timeout``."""
    started = time.monotonic()
    while not condition():
        assert time.monotonic() - started < timeout, "condition never held"
        time.sleep(0.01)
    return time.monotonic() - started


def write_env_file(
    root: Path,
    base_url: str,
    api_key: str,
    model: str | None = None,
    request_timeout: float | None = None,
    concurrency: int | None = None,
) -> Path:
    """Write an environment file for one endpoint under ``root``; return its path."""
    path = root / f"model-{uuid.uuid4().hex[:6]}.env"
    path.write_text(
        f"OPENAI_BASE_URL={base_url}\n"
        f"OPENAI_API_KEY={api_key}\n"
        + (f"AI_DEFAULT_MODEL={model}\n" if model else "")
        + (f"ZT_REQUEST_TIMEOUT_SECONDS={request_timeout}\n" if request_timeout else "")
        + (f"ZT_CONCURRENCY={concurrency}\n" if concurrency else ""),
        encoding="utf-8",
    )
    return path


def launch_service(
    model: FakeModel | str | None,
    root: Path,
    concurrency: int = 4,
    env_file: Path | None = None,
    data_dir: Path | None = None,
    request_timeout: float | None = None,
    argon: FakeModel | str | None = None,
    argon_env_file: Path | None = None,
    argon_concurrency: int | None = None,
    env: dict[str, str] | None = None,
    hedging: bool = True,
) -> ServiceClient:
    """Start the service subprocess on a random port with its data under ``root``.

    Flash is ``model`` (the fake, or an endpoint URL), or the endpoint in
    ``env_file``. Argon is ``argon``, or the endpoint in ``argon_env_file``,
    or else ``model`` again; its requests carry the default argon model name
    and ``FAKE_ARGON_API_KEY``. Pass ``data_dir`` to restart on earlier data,
    ``request_timeout`` (seconds) to replace the 480-second request timeout of
    both endpoints, and ``argon_concurrency`` to replace argon's limit of 16.
    ``env`` adds environment variables to the service's (the ``ZT_TEST_``
    settings). ``hedging=False`` starts it with ``--no-hedging``. The
    client's ``output`` collects the service's whole standard output.
    """
    data_dir = data_dir or root / f"data-{uuid.uuid4().hex[:6]}"
    if env_file is None:
        base_url = model if isinstance(model, str) else model.base_url
        env_file = write_env_file(root, base_url, FAKE_API_KEY, FAKE_MODEL_NAME, request_timeout)
    if argon_env_file is None:
        endpoint = argon if argon is not None else model
        base_url = endpoint if isinstance(endpoint, str) else endpoint.base_url
        argon_env_file = write_env_file(
            root, base_url, FAKE_ARGON_API_KEY, None, request_timeout, argon_concurrency
        )
    process = subprocess.Popen(
        serve_command(env_file, argon_env_file, data_dir, concurrency=concurrency)
        + ([] if hedging else ["--no-hedging"]),
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, **(env or {})},
        # Its own process group, inherited by its task subprocesses: one
        # signal ends them all, and a Ctrl-C reaches only pytest, which then
        # tears the fixtures down.
        start_new_session=True,
    )
    line = process.stdout.readline()
    assert line, "service exited before announcing its address"
    output = [line.rstrip("\n")]
    # Keep reading, so that the service never blocks on a full pipe.
    threading.Thread(
        target=lambda: output.extend(line.rstrip("\n") for line in process.stdout),
        daemon=True,
    ).start()
    return ServiceClient(json.loads(line)["url"], process, data_dir, output)


def serve_command(
    env_file: Path, argon_env_file: Path, data_dir: Path, port: int = 0, concurrency: int = 4
) -> list[str]:
    """The command line that starts the translation service; port 0 picks a free port."""
    return [
        sys.executable,
        "-m",
        "zotero_translate",
        "serve",
        "--port",
        str(port),
        "--data-dir",
        str(data_dir),
        "--env-file",
        str(env_file),
        "--argon-env-file",
        str(argon_env_file),
        "--concurrency",
        str(concurrency),
    ]


def stop_service(client: ServiceClient) -> None:
    """End the service and every task process it started; return when all have exited.

    The service's process group is signalled first. Task subprocesses run
    in process groups of their own (M3 ticket 04) and end themselves and
    their children when the service is gone, but that check has failed to
    run at least once (a worker left running for 31 minutes after a test),
    so every process with a process marker of a task in the service's data
    directory is then ended too, whatever its group.
    """
    group = client.process.pid  # the leader of its own session and process group
    for sig, grace in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 10.0)):
        _signal_group(group, sig)
        try:
            client.process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
        if _group_ended(group, timeout=grace):
            break
    else:
        raise AssertionError(f"processes of the service group {group} survived SIGKILL")
    data_dir = getattr(client, "data_dir", None)
    if data_dir is not None:
        _end_marked_processes(data_dir)


def marked_processes(task_ids) -> list[psutil.Process]:
    """Live processes whose process marker names one of ``task_ids``."""
    prefixes = tuple(f"{task_id}:" for task_id in task_ids)
    found = []
    if not prefixes:
        return found
    for process in psutil.process_iter():
        try:
            if process.environ().get(MARK_ENV, "").startswith(prefixes) and (
                process.status() != psutil.STATUS_ZOMBIE
            ):
                found.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return found


def _end_marked_processes(data_dir: Path) -> None:
    """End every process of a task under ``data_dir``: SIGTERM, then SIGKILL."""
    tasks = Path(data_dir) / "tasks"
    task_ids = [p.name for p in tasks.iterdir()] if tasks.is_dir() else []
    for method in (psutil.Process.terminate, psutil.Process.kill):
        left = marked_processes(task_ids)
        if not left:
            return
        for process in left:
            try:
                method(process)
            except psutil.NoSuchProcess:
                pass
        deadline = time.monotonic() + 10
        while marked_processes(task_ids) and time.monotonic() < deadline:
            time.sleep(0.05)
    left = marked_processes(task_ids)
    assert not left, f"task processes survived SIGKILL: {[p.pid for p in left]}"


def _signal_group(group: int, sig: signal.Signals) -> None:
    try:
        os.killpg(group, sig)
    except (ProcessLookupError, PermissionError):  # see _group_ended
        pass


def _group_ended(group: int, timeout: float) -> bool:
    """Wait until no process of ``group`` runs; False if one still does after ``timeout``.

    A process that has exited but is not yet reaped (a zombie, e.g. a task
    subprocess whose service is gone) no longer runs. macOS answers
    ``killpg(group, 0)`` with EPERM rather than ESRCH when no member of the
    group can take a signal: when all are zombies, but also as soon as the
    last live one has begun to exit, while ``ps`` still shows it (state
    ``?E``) and heavy load can keep it so for a while. On EPERM the group has
    ended only when ``ps`` shows every member as a zombie.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            if all(state.startswith("Z") for state in _member_states(group)):
                return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)


def _member_states(group: int) -> list[str]:
    """The ``ps`` state of every process in ``group``, zombies included."""
    listing = subprocess.run(
        ["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True, check=True
    ).stdout
    members = (line.split() for line in listing.splitlines())
    return [state for pgid, state in members if int(pgid) == group]


def pytest_configure(config) -> None:
    # SIGTERM (a killed test run) interrupts pytest like Ctrl-C, so the
    # fixtures are torn down and stop their services; by default Python
    # would exit at once and leave the services running.
    signal.signal(signal.SIGTERM, signal.default_int_handler)


@pytest.fixture
def fake_model():
    with FakeModel() as model:
        yield model


@pytest.fixture
def start_service(tmp_path):
    """Return ``start(fake_model, concurrency=4, data_dir=None, request_timeout=None, argon=None,
    argon_concurrency=None, env=None, hedging=True)``; see ``launch_service``."""
    started: list[ServiceClient] = []

    def start(
        model,
        concurrency: int = 4,
        data_dir: Path | None = None,
        request_timeout: float | None = None,
        argon=None,
        argon_concurrency: int | None = None,
        env: dict[str, str] | None = None,
        hedging: bool = True,
    ) -> ServiceClient:
        client = launch_service(
            model,
            tmp_path,
            concurrency,
            data_dir=data_dir,
            request_timeout=request_timeout,
            argon=argon,
            argon_concurrency=argon_concurrency,
            env=env,
            hedging=hedging,
        )
        started.append(client)
        return client

    yield start
    for client in started:
        stop_service(client)
