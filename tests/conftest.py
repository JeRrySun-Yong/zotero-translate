"""Shared pytest fixtures: the fake pipeline model and a running translation service.

The only test seam is the translation service HTTP interface. Each service
fixture starts the service as a subprocess on a random port, with a
temporary data directory and two environment files: one points flash (the
translation endpoint) at the fake model, the other points argon (the paper
dossier and term list endpoint, ADR 0004) at a second fake model or, when
the test gives none, at the same one.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from tests.fake_model import FakeModel

FIXTURES = Path(__file__).parent / "fixtures"
EXCERPT = FIXTURES / "autodesign-p1.pdf"
FAKE_MODEL_NAME = "fake-gemini-for-tests"
FAKE_API_KEY = "sk-fake-test-key-0123456789"
FAKE_ARGON_API_KEY = "sk-fake-argon-key-9876543210"
ARGON_MODEL_NAME = "gemini-4-argon-high"  # the default, since the argon file names no model
TERMINAL_STATES = {"succeeded", "failed"}


class ServiceClient:
    """Talks to a running translation service over HTTP, like the plugin does."""

    def __init__(self, base_url: str, process: subprocess.Popen, data_dir: Path):
        self.base_url = base_url
        self.process = process
        self.data_dir = data_dir
        self.http = httpx.Client(base_url=base_url, timeout=30)

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
) -> ServiceClient:
    """Start the service subprocess on a random port with its data under ``root``.

    Flash is ``model`` (the fake, or an endpoint URL), or the endpoint in
    ``env_file``. Argon is ``argon``, or the endpoint in ``argon_env_file``,
    or else ``model`` again; its requests carry the default argon model name
    and ``FAKE_ARGON_API_KEY``. Pass ``data_dir`` to restart on earlier data,
    ``request_timeout`` (seconds) to replace the 480-second request timeout of
    both endpoints, and ``argon_concurrency`` to replace argon's limit of 16.
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
        [
            sys.executable,
            "-m",
            "zotero_translate",
            "serve",
            "--port",
            "0",
            "--data-dir",
            str(data_dir),
            "--env-file",
            str(env_file),
            "--argon-env-file",
            str(argon_env_file),
            "--concurrency",
            str(concurrency),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    line = process.stdout.readline()
    assert line, "service exited before announcing its address"
    return ServiceClient(json.loads(line)["url"], process, data_dir)


def stop_service(client: ServiceClient) -> None:
    client.process.terminate()
    try:
        client.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        client.process.kill()


@pytest.fixture
def fake_model():
    with FakeModel() as model:
        yield model


@pytest.fixture
def start_service(tmp_path):
    """Return ``start(fake_model, concurrency=4, data_dir=None, request_timeout=None, argon=None,
    argon_concurrency=None)``; see ``launch_service``."""
    started: list[ServiceClient] = []

    def start(
        model,
        concurrency: int = 4,
        data_dir: Path | None = None,
        request_timeout: float | None = None,
        argon=None,
        argon_concurrency: int | None = None,
    ) -> ServiceClient:
        client = launch_service(
            model,
            tmp_path,
            concurrency,
            data_dir=data_dir,
            request_timeout=request_timeout,
            argon=argon,
            argon_concurrency=argon_concurrency,
        )
        started.append(client)
        return client

    yield start
    for client in started:
        stop_service(client)
