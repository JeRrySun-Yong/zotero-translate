"""Shared pytest fixtures: the fake pipeline model and a running translation service.

The only test seam is the translation service HTTP interface. Each service
fixture starts the service as a subprocess on a random port, with a
temporary data directory and an environment file that points the pipeline
model at the fake model.
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
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status(task_id)
            if on_poll:
                on_poll(status)
            if status["state"] in TERMINAL_STATES:
                return status
            time.sleep(0.2)
        raise AssertionError(f"task {task_id} did not finish in {timeout}s")

    def download(self, task_id: str, name: str) -> bytes:
        response = self.http.get(f"/tasks/{task_id}/files/{name}")
        assert response.status_code == 200, response.text
        return response.content


def launch_service(model: FakeModel, root: Path, concurrency: int = 4) -> ServiceClient:
    """Start the service subprocess on a random port; data and env file under ``root``."""
    data_dir = root / f"data-{uuid.uuid4().hex[:6]}"
    env_file = root / f"model-{uuid.uuid4().hex[:6]}.env"
    env_file.write_text(
        f"OPENAI_BASE_URL={model.base_url}\n"
        f"OPENAI_API_KEY={FAKE_API_KEY}\n"
        f"AI_DEFAULT_MODEL={FAKE_MODEL_NAME}\n",
        encoding="utf-8",
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
    """Return ``start(fake_model, concurrency=4) -> ServiceClient``."""
    started: list[ServiceClient] = []

    def start(model: FakeModel, concurrency: int = 4) -> ServiceClient:
        client = launch_service(model, tmp_path, concurrency)
        started.append(client)
        return client

    yield start
    for client in started:
        stop_service(client)
