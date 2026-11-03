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
