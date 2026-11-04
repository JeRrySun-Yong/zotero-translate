"""A worker that reported its result but does not exit (M3 ticket 13, full-suite hang).

Seen once in a full suite and once in repeated runs: the paper task finished,
the worker sent its result, and then the process stayed alive past Python's
own shutdown, in a native library's exit handler (no faulthandler dump on
SIGABRT; see the ticket). The service waited for it without a limit, so the
task stayed ``running``. Now the service waits a bounded time once it has a
result, then ends the worker's processes by marker and adopts the result.

``ZT_TEST_WORKER_HANG_AT_EXIT=1`` makes the worker register a C exit handler
that never returns, which reproduces that state; ``ZT_TEST_WORKER_EXIT_GRACE_SECONDS``
shortens the service's wait.
"""

import time

from tests.conftest import launch_service
from tests.conftest import marked_processes
from tests.conftest import stop_service
from tests.fake_model import FakeModel


def test_a_worker_hanging_at_exit_after_its_result_is_ended_and_the_task_succeeds(tmp_path):
    with FakeModel() as model:
        service = launch_service(
            model,
            tmp_path,
            env={"ZT_TEST_WORKER_HANG_AT_EXIT": "1", "ZT_TEST_WORKER_EXIT_GRACE_SECONDS": "2"},
        )
        try:
            task_id = service.submit()
            started = time.monotonic()
            final = service.wait(task_id, timeout=90)
            assert final["state"] == "succeeded", final
            assert time.monotonic() - started < 90
            assert not marked_processes([task_id])  # the hung worker was ended
            log = (service.data_dir / "tasks" / task_id / "task.log").read_text("utf-8")
            assert "did not exit" in log
        finally:
            stop_service(service)
