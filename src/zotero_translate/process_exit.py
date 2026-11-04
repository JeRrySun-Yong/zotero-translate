"""End a process without running native libraries' exit handlers (M3 ticket 17).

A worker sometimes hangs after Python's own shutdown has finished, in a
native library's exit handler (a C ``atexit`` handler or a C++ static
destructor; onnxruntime and CoreML's ANE service thread are present, the
cause is unknown). The service would then wait for it without limit. The
worker and ``run`` therefore end through ``run_then_exit``: it does what
Python's normal exit does that matters to us, then ends the process with
``os._exit``, which skips the interpreter's teardown and every native exit
handler.

What still happens, in the order of a normal exit:

1. the threads that are not daemons are joined, after
   ``threading``'s own exit hooks (``concurrent.futures`` wakes its idle
   workers): a request still in flight when the paper task ended (the model
   client cancels them on close) appends its record to ``requests.jsonl``
   from such a thread;
2. Python's exit handlers (``atexit``): ``multiprocessing`` ends its
   daemonic child processes and joins the others (the engine's
   subprocesses), the engine joins its thread pools, ``run`` brings
   ``report.json``'s ``requests`` up to date with ``requests.jsonl``
   (``zotero_translate.request_log.update_report``), ``logging`` flushes;
3. standard output and standard error are flushed.

Each request record is appended with its own ``open`` and ``close``, and
the report is written whole before ``os._exit``: the files are complete in
the operating system, which ``os._exit`` does not undo.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
from typing import Callable
from typing import NoReturn


def run_then_exit(main: Callable[[], object]) -> NoReturn:
    """Run ``main``, then end the process with its exit status, as ``SystemExit`` would.

    ``main`` returns the exit status (None means 0), or raises: ``SystemExit``
    gives its code, any other exception is printed and gives 1.
    """
    try:
        status = main()
    except SystemExit as e:
        status = e.code
    except BaseException:  # noqa: BLE001 - printed as Python would, then exit 1
        sys.excepthook(*sys.exc_info())
        status = 1
    _exit_now(_exit_code(status))


def _exit_now(code: int) -> NoReturn:
    """End the process with ``code`` after the steps in the module docstring.

    Call it from the main thread, as the last thing the process does.
    """
    threading._shutdown()  # what the interpreter calls first on exit
    atexit._run_exitfuncs()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - a closed stream must not stop the exit
            pass
    os._exit(code)


def _exit_code(status: object) -> int:
    """The exit code ``SystemExit(status)`` would give."""
    if status is None:
        return 0
    if isinstance(status, int):
        return status & 0xFF
    print(status, file=sys.stderr)
    return 1
