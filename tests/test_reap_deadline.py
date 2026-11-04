"""The unified reap keeps the rule's 15 seconds across its rescans (spec section 4).

A process that ignores the terminate signal and starts a new marked process
each time it gets one forces the reap to scan again. However many rounds
that takes, the reap ends (confirmed or failed) within about 15 seconds.
"""

import os
import subprocess
import sys
import time
import uuid

from zotero_translate import processes

# Ignores SIGTERM; on each SIGTERM starts a copy of itself (same marker), then sleeps on.
_STUBBORN = r"""
import os, signal, subprocess, sys, time
def on_term(signum, frame):
    subprocess.Popen([sys.executable, "-c", os.environ["ZT_TEST_STUBBORN_CODE"]], start_new_session=True)
signal.signal(signal.SIGTERM, on_term)
open(sys.argv[1], "w").close()
while True:
    time.sleep(0.05)
"""


def test_a_reap_whose_processes_keep_starting_new_ones_ends_within_the_rules_15_seconds(tmp_path):
    task_id = f"reap-deadline-{uuid.uuid4().hex[:8]}"
    ready = tmp_path / "ready"
    env = {
        **os.environ,
        processes.MARK_ENV: f"{task_id}:1",
        "ZT_TEST_STUBBORN_CODE": _STUBBORN.replace("sys.argv[1]", repr(str(tmp_path / "child-ready"))),
    }
    first = subprocess.Popen([sys.executable, "-c", _STUBBORN, str(ready)], env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 30
        while not (ready.exists() and processes.marked_processes(task_id)):
            assert time.monotonic() < deadline, "the stubborn process never started"
            time.sleep(0.05)

        started = time.monotonic()
        try:
            processes.reap(task_id)
            outcome = "confirmed"
        except processes.ReapError:
            outcome = "failed"
        elapsed = time.monotonic() - started

        assert elapsed < processes.TERM_WAIT_SECONDS + processes.KILL_WAIT_SECONDS + 2, (
            f"the reap took {elapsed:.1f} s ({outcome}); the rule allows about 15 s"
        )
        if outcome == "confirmed":
            assert processes.marked_processes(task_id) == []
    finally:
        for process in processes.marked_processes(task_id):
            try:
                process.kill()
            except Exception:
                pass
        first.kill()
        first.wait()
