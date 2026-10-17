"""Spec behaviour 2: tasks run first in, first out, one at a time."""

import time

from tests.conftest import TERMINAL_STATES


def test_second_task_waits_in_queue_until_the_first_finishes(fake_model, start_service):
    fake_model.delay = 0.2
    service = start_service(fake_model)

    first = service.submit()
    second = service.submit()

    snapshots = []
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        pair = (service.status(first), service.status(second))
        snapshots.append(pair)
        if all(s["state"] in TERMINAL_STATES for s in pair):
            break
        time.sleep(0.2)

    assert [s["state"] for s in snapshots[-1]] == ["succeeded", "succeeded"]
    for a, b in snapshots:
        assert not (a["state"] == "running" and b["state"] == "running")
        assert b["state"] != "running" or a["state"] == "succeeded", "second ran before first"
    assert any(
        a["state"] == "running" and b["state"] == "queued" and b["queue_position"] == 1
        and b["stage"] == "排队中"
        for a, b in snapshots
    ), "the second task was never seen waiting in the queue"
    assert any(b["state"] == "running" for _, b in snapshots)
