"""Finding and ending the processes of a paper task (spec section 2).

Every process of a task attempt carries the process marker
``<task_id>:<attempt>`` in the environment variable ``ZT_PROCESS_MARK``: the
service passes it to the worker when it starts it, and every process the
worker starts inherits it, the engine's spawned subprocesses and resource
tracker included. ``reap`` ends every process that carries a marker of one
task, of any attempt, by the one reaping rule: the termination signal, at
most 10 seconds' wait, then a forced kill and at most 5 more seconds.

A process whose environment cannot be read (``AccessDenied``: another user's
process, a setuid program) is skipped: a worker and its subprocesses are
ordinary processes of the service's user (ticket 02). A process that has
exited but is not yet reaped by its parent (a zombie) counts as exited.
Reaping fails only when the scan itself fails, or when a marked process
still lives after the forced kill.

Test only: while the file named by ``ZT_TEST_REAP_SCAN_ERROR`` exists, the
scan fails.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import psutil

MARK_ENV = "ZT_PROCESS_MARK"
SCAN_ERROR_SETTING = "ZT_TEST_REAP_SCAN_ERROR"  # test only
TERM_WAIT_SECONDS = 10.0
KILL_WAIT_SECONDS = 5.0


class ReapError(Exception):
    """The processes of a task could not be confirmed to have exited."""


def marker(task_id: str, attempt: int) -> str:
    return f"{task_id}:{attempt}"


def marked_processes(task_id: str) -> list[psutil.Process]:
    """The live processes, other than this one, with a process marker of ``task_id``."""
    prefix = f"{task_id}:"
    found = []
    for process in psutil.process_iter():
        if process.pid == os.getpid():
            continue
        try:
            if process.environ().get(MARK_ENV, "").startswith(prefix) and _alive(process):
                found.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return found


def reap(task_id: str) -> None:
    """End every process of ``task_id``; raise ``ReapError`` if that cannot be confirmed.

    A process may start a marked child after a scan and before it is
    ended, so the scan is repeated after each round until it finds none;
    when marked processes still turn up after the rule's 15 seconds, the
    reap fails.
    """
    deadline = time.monotonic() + TERM_WAIT_SECONDS + KILL_WAIT_SECONDS
    while True:
        processes = _scan(task_id)
        if not processes:
            return
        if time.monotonic() >= deadline:
            pids = ", ".join(str(p.pid) for p in processes)
            raise ReapError(f"回收期间不断出现新的带标记的进程（PID {pids}）")
        survivors = end(processes)
        if survivors:
            pids = ", ".join(str(p.pid) for p in survivors)
            raise ReapError(f"强制结束后仍有带标记的进程存活（PID {pids}）")


def _scan(task_id: str) -> list[psutil.Process]:
    try:
        flag = os.environ.get(SCAN_ERROR_SETTING)
        if flag and Path(flag).exists():
            raise OSError(f"测试设置 {SCAN_ERROR_SETTING}")
        return marked_processes(task_id)
    except Exception as e:  # the scan itself failed
        raise ReapError(f"无法扫描进程：{type(e).__name__}: {e}") from e


def end(
    processes: list[psutil.Process],
    term_wait: float = TERM_WAIT_SECONDS,
    kill_wait: float = KILL_WAIT_SECONDS,
) -> list[psutil.Process]:
    """Terminate ``processes``, then kill the ones left; return those still alive."""
    for sig_method, wait in ((psutil.Process.terminate, term_wait), (psutil.Process.kill, kill_wait)):
        for process in processes:
            try:
                sig_method(process)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
            except psutil.AccessDenied:
                pass  # still alive: it shows up as a survivor
        processes = _wait_gone(processes, wait)
        if not processes:
            return []
    return processes


def _wait_gone(processes: list[psutil.Process], timeout: float) -> list[psutil.Process]:
    deadline = time.monotonic() + timeout
    while True:
        processes = [p for p in processes if _alive(p)]
        if not processes or time.monotonic() >= deadline:
            return processes
        time.sleep(0.05)


def _alive(process: psutil.Process) -> bool:
    """True while ``process`` runs; an exited process not yet reaped (a zombie) does not."""
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True


def group_members() -> list[psutil.Process]:
    """The live processes, other than this one, of this process's process group."""
    me = os.getpid()
    group = os.getpgid(0)
    members = []
    for process in psutil.process_iter():
        # PID 0 (the kernel on macOS) would name this process's own group to getpgid.
        if process.pid in (0, me):
            continue
        try:
            if os.getpgid(process.pid) == group and _alive(process):
                members.append(process)
        except OSError:  # gone, or in another session
            continue
    return members
