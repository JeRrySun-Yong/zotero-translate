"""Test only: make a Python process hang after its own shutdown, in a C exit handler.

Put this directory on ``PYTHONPATH`` and set ``ZT_TEST_HANG_AT_EXIT=1``: the
first Python process started that way registers a C exit handler that never
returns, as a native library's teardown sometimes does in the worker (M3
tickets 13 and 17). A process that ends with ``os._exit`` skips it. The
setting is taken out of the environment, so the processes it starts (the
engine's subprocesses) exit normally.
"""

import ctypes
import os

if os.environ.pop("ZT_TEST_HANG_AT_EXIT", None):
    _libc = ctypes.CDLL(None)
    _libc.atexit(ctypes.cast(_libc.pause, ctypes.c_void_p))
