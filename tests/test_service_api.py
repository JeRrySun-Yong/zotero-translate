"""Translation service HTTP interface: health, unknown tasks, loopback only,
the test-only delay before a download."""

import json
import shutil
import subprocess
import threading
import time
import uuid

import httpx
import pytest

from tests.conftest import EXCERPT


def test_health_names_the_service_and_its_version(fake_model, start_service):
    service = start_service(fake_model)

    response = service.http.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "zotero-translate"
    assert body["version"]


@pytest.mark.parametrize(
    "path", ["/tasks/no-such-task", "/tasks/no-such-task/files/mono", "/tasks/no-such-task/files/dual"]
)
def test_unknown_task_id_returns_404(fake_model, start_service, path):
    service = start_service(fake_model)

    assert service.http.get(path).status_code == 404


def test_service_listens_on_loopback_only(fake_model, start_service):
    service = start_service(fake_model)
    port = httpx.URL(service.base_url).port
    if shutil.which("lsof") is None:
        pytest.skip("lsof is not available")

    listing = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(service.process.pid), f"-iTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
    ).stdout

    listening = [line.split()[8] for line in listing.splitlines()[1:]]
    assert listening == [f"127.0.0.1:{port}"]


def test_a_test_only_delay_holds_back_one_file_before_it_is_sent(fake_model, start_service, tmp_path):
    """``ZT_TEST_DOWNLOAD_DELAY_SECONDS="dual=<seconds>"`` (M3 ticket 10): the
    dual PDF is sent only after the delay; mono and the task's status are not held up."""
    data_dir = tmp_path / "data"
    task_id = uuid.uuid4().hex  # no other test run on the machine has its process marker
    directory = data_dir / "tasks" / task_id
    directory.mkdir(parents=True)
    shutil.copy(EXCERPT, directory / "source.pdf")
    (directory / "task.json").write_text(
        json.dumps({"task_id": task_id, "filename": "p.pdf", "submitted_at": time.time()}), encoding="utf-8"
    )
    (directory / "status.json").write_text(
        json.dumps({"state": "succeeded", "stage": "完成", "progress": 100, "error": None}), encoding="utf-8"
    )
    (directory / "mono.pdf").write_bytes(b"%PDF-mono")
    (directory / "dual.pdf").write_bytes(b"%PDF-dual")
    service = start_service(fake_model, data_dir=data_dir, env={"ZT_TEST_DOWNLOAD_DELAY_SECONDS": "dual=2"})

    started = time.monotonic()
    assert service.download(task_id, "mono") == b"%PDF-mono"
    assert time.monotonic() - started < 1.5

    answers = []
    with httpx.Client(base_url=service.base_url, timeout=30) as other:
        started = time.monotonic()
        download = threading.Thread(target=lambda: answers.append(other.get(f"/tasks/{task_id}/files/dual")))
        download.start()
        time.sleep(0.5)
        assert service.status(task_id)["state"] == "succeeded"  # answered while dual waits
        assert answers == []
        download.join()
    assert time.monotonic() - started >= 2
    assert answers[0].content == b"%PDF-dual"
