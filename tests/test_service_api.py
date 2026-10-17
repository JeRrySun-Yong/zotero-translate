"""Translation service HTTP interface: health, unknown tasks, loopback only."""

import shutil
import subprocess

import httpx
import pytest


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
