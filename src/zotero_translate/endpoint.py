"""Model endpoint: one HTTP client, one concurrency budget and cancellable sends (spec section 7).

``Endpoint.start`` waits for a slot of the endpoint's budget and then sends
one request in its own thread; the ``Send`` it returns can be cancelled at
any stage:

- while connecting: the connector (``connect`` by default) is handed the
  send's cancel event and gives up when it is set; a connector that does
  not look at it is still safe, because a cancelled send closes the
  connection it gets without writing anything;
- while waiting for the response headers and while reading the body: the
  send's socket is shut down (``shutdown``, not ``close``, so that the
  thread blocked on it wakes up), and the send's thread then closes the
  connection itself. Only this send's connection is touched: the other sends
  on the endpoint and the shared HTTP client go on.

A send's slot is given back only after its local connection has ended:
closed, or handed back to the pool after a whole response. Until then the
send counts as in flight (``Endpoint.in_flight``), also after its caller
gave up on it (the request timeout cancels it; see
``zotero_translate.model_client``). What this controls is the local
connection, not whether the remote model has stopped working on the request.

The cancellation reaches the socket through httpcore's network backend:
``_Backend`` makes the connections and ``_Stream`` wraps each socket and
ties it to the send whose thread is using it (a pooled connection serves
one send after another). httpx has no public way to pass a network
backend, so ``_use_backend`` sets it on its connection pools; it fails at
once if a new httpx or httpcore changes that.

``on_event(name, send)`` lets a test watch the order of things: ``connected``
(the connector returned a socket for the send), ``connection_returned``
(the send read its whole response and gave up its connection, to the pool
or to be closed), ``connection_closed`` (a connection closed while the
send's exchange used it; ``send`` is None for one no send was using) and
``slot_released`` (the send's slot is back; nothing of it follows).

``close`` cancels every send in flight, waits a little for them to end and
closes the HTTP client; a send that starts afterwards fails with
``SendCancelled``.
"""

from __future__ import annotations

import errno
import itertools
import select
import socket
import threading
import time
from typing import Callable

import httpcore
import httpx

from zotero_translate.settings import ModelSettings

# connector(host, port, timeout, cancelled) -> a connected socket
Connector = Callable[[str, int, "float | None", threading.Event], socket.socket]
EventCallback = Callable[[str, "Send | None"], None]

CONNECT_POLL_SECONDS = 0.05  # how often a connect looks at its cancel event
CLOSE_WAIT_SECONDS = 5.0  # how long ``close`` waits for the cancelled sends to end

_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN}  # a non-blocking connect under way
_current = threading.local()
# socket.socket's own shutdown (from _socket.socket); ssl.SSLSocket overrides it.
_SOCKET_SHUTDOWN = socket.socket.shutdown  # .send: the send whose thread this is


class SendCancelled(Exception):
    """The send was cancelled, or its endpoint closed, before it got an answer."""


def connect(host: str, port: int, timeout: float | None, cancelled: threading.Event) -> socket.socket:
    """Open a TCP connection to ``host:port``; give up when ``cancelled`` is set.

    The name lookup itself cannot be interrupted; the connect can.
    Raises ``SendCancelled``, ``socket.timeout`` or ``OSError``.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    error: OSError | None = None
    for family, kind, proto, _, address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        sock = socket.socket(family, kind, proto)
        try:
            sock.setblocking(False)
            code = sock.connect_ex(address)
            while code != 0:
                if code not in _IN_PROGRESS:
                    raise OSError(code, f"connect to {host}:{port} failed")
                if cancelled.is_set():
                    raise SendCancelled("模型请求在建立连接时被中止")
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    raise socket.timeout(f"connect to {host}:{port} timed out")
                wait = CONNECT_POLL_SECONDS if left is None else min(CONNECT_POLL_SECONDS, left)
                _, writable, failed = select.select([], [sock], [sock], wait)
                if writable or failed:
                    code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if code != 0:
                        raise OSError(code, f"connect to {host}:{port} failed")
            sock.setblocking(True)
            return sock
        except OSError as e:  # socket.timeout included; try the next address
            sock.close()
            if isinstance(e, socket.timeout):
                raise
            error = e
        except BaseException:
            sock.close()
            raise
    raise error or OSError(f"cannot resolve {host}")


class Send:
    """One send in flight on an endpoint; see the module docstring."""

    _ids = itertools.count(1)

    def __init__(self, endpoint: Endpoint):
        self.id = next(self._ids)
        self.slot_wait_seconds = 0.0
        self.started_at = time.time()  # wall clock; set again as the request goes out
        self.sent = time.monotonic()  # monotonic clock; likewise
        self.cancelled = threading.Event()
        self._endpoint = endpoint
        self._lock = threading.Lock()
        self._stream: _Stream | None = None  # the connection it is using
        self._exchanging = True  # until its HTTP exchange is over
        self._ended = threading.Event()  # connection ended and slot given back
        self._response: tuple[int, bytes] | None = None
        self._error: BaseException | None = None

    def __repr__(self) -> str:
        return f"<Send {self.id}>"

    def cancel(self) -> None:
        """Abort the send at whatever stage it is; return at once.

        The send then ends as soon as its connection is closed; ``wait``
        tells when. Only a connection the send is still using is shut down:
        once its exchange is over, the connection may serve another send.
        """
        with self._lock:
            self.cancelled.set()
            stream = self._stream
        if stream is not None:
            stream.shutdown_for(self)

    def wait(self, timeout: float | None = None) -> bool:
        """True once the send has ended: its connection ended and its slot is back."""
        return self._ended.wait(timeout)

    def result(self) -> tuple[int, bytes]:
        """The status code and whole body, once ended; raises how the send failed.

        A cancelled send that got no whole response raises ``SendCancelled``.
        """
        assert self._ended.is_set(), "the send has not ended"
        if self._response is not None:
            return self._response
        if self.cancelled.is_set():
            raise SendCancelled("模型请求已中止")
        assert self._error is not None
        raise self._error

    def _attach(self, stream: _Stream) -> None:
        """The send's thread uses ``stream``; refuse if the send is cancelled."""
        with self._lock:
            if self.cancelled.is_set():
                raise SendCancelled("模型请求已中止")
            if self._exchanging:
                self._stream = stream

    @property
    def exchanging(self) -> bool:
        with self._lock:
            return self._exchanging

    def _exchange(self, http: httpx.Client, path: str, body: dict, timeout: float | None) -> None:
        """Send the request and read the whole response, in the send's own thread.

        The exchange is over before the connection goes back to the pool:
        from then on, a cancel of this send no longer reaches it.
        """
        self.started_at = time.time()
        self.sent = time.monotonic()
        try:
            with http.stream("POST", path, json=body, timeout=timeout) as response:
                data = b"".join(response.iter_bytes())
                self._response = (response.status_code, data)
                self._exchange_over()
            self._endpoint._event("connection_returned", self)
        except BaseException as e:  # noqa: BLE001 - the caller gets it from ``result``
            self._error = e
        finally:
            # The connection is closed now (on an error, httpx closes it as
            # the response context exits), or back in the pool.
            self._exchange_over()

    def _exchange_over(self) -> None:
        with self._lock:
            self._exchanging = False
            stream, self._stream = self._stream, None
        if stream is not None:
            stream.disown(self)

    def _end(self) -> None:
        """The send's slot is back: it has ended."""
        self._ended.set()


class Endpoint:
    """One endpoint's HTTP client and concurrency budget, shared by the clients that use it.

    The budget holds ``settings.concurrency`` slots. ``connector`` and
    ``on_event`` are for tests (see the module docstring).
    """

    def __init__(
        self,
        settings: ModelSettings,
        connector: Connector = connect,
        on_event: EventCallback | None = None,
    ):
        self.concurrency = settings.concurrency
        self.connector = connector
        self._on_event = on_event
        self._budget = threading.Condition()
        self._in_flight = 0
        self.max_in_flight = 0
        self._sends: set[Send] = set()
        self._closed = False
        self._http = httpx.Client(
            base_url=settings.base_url,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            # The pool is not capped: the budget caps the sends, and a send
            # holds its connection until the connection has ended.
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=settings.concurrency),
        )
        _use_backend(self._http, _Backend(self))

    @property
    def in_flight(self) -> int:
        """Sends holding a slot: those whose connection has not ended yet."""
        with self._budget:
            return self._in_flight

    def start(
        self,
        path: str,
        body: dict,
        timeout: float | None = None,
        on_start: Callable[[], None] | None = None,
        on_end: Callable[[], None] | None = None,
    ) -> Send:
        """Wait for a slot, then send ``body`` to ``path`` in the send's own thread.

        ``timeout`` bounds each network operation (httpx's timeout); the
        caller bounds the whole send by cancelling it. ``on_start`` runs as
        the slot is taken, ``on_end`` as it is given back. Raises
        ``SendCancelled`` if the endpoint closes (or is closed) first.
        """
        waiting = time.monotonic()
        with self._budget:
            while not self._closed and self._in_flight >= self.concurrency:
                self._budget.wait()
            if self._closed:
                raise SendCancelled("模型客户端已关闭")
            send = Send(self)
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            self._sends.add(send)
            if on_start is not None:
                on_start()
        send.slot_wait_seconds = time.monotonic() - waiting
        thread = threading.Thread(
            target=self._run, args=(send, path, body, timeout, on_end), name="model-request", daemon=True
        )
        thread.start()
        return send

    def _run(self, send: Send, path: str, body: dict, timeout, on_end) -> None:
        _current.send = send
        try:
            send._exchange(self._http, path, body, timeout)
        finally:
            # The send's connection has ended: only now is its slot given back.
            _current.send = None
            with self._budget:
                self._in_flight -= 1
                self._sends.discard(send)
                if on_end is not None:
                    on_end()
                self._budget.notify_all()
            self._event("slot_released", send)
            send._end()

    def _event(self, name: str, send: Send | None) -> None:
        if self._on_event is not None:
            self._on_event(name, send)

    def close(self) -> None:
        """Cancel every send in flight, wait a little for them, close the HTTP client."""
        with self._budget:
            self._closed = True
            sends = list(self._sends)
            self._budget.notify_all()
        for send in sends:
            send.cancel()
        deadline = time.monotonic() + CLOSE_WAIT_SECONDS
        for send in sends:
            send.wait(max(0.0, deadline - time.monotonic()))
        self._http.close()


def _use_backend(http: httpx.Client, backend: httpcore.NetworkBackend) -> None:
    """Make every connection pool of ``http`` (proxies included) connect through ``backend``."""
    transports = [http._transport, *(t for t in http._mounts.values() if t is not None)]
    for transport in transports:
        pool = getattr(transport, "_pool", None)
        if pool is None or not hasattr(pool, "_network_backend"):
            raise RuntimeError("httpx 或 httpcore 的内部结构变了：无法设置可取消的连接")
        pool._network_backend = backend


class _Backend(httpcore.NetworkBackend):
    """Connections made with the endpoint's connector, each tied to the send using it."""

    def __init__(self, endpoint: Endpoint):
        self._endpoint = endpoint

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        send = getattr(_current, "send", None)
        cancelled = send.cancelled if send is not None else threading.Event()
        try:
            sock = self._endpoint.connector(host, port, timeout, cancelled)
        except socket.timeout as e:
            raise httpcore.ConnectTimeout(str(e)) from e
        except OSError as e:
            raise httpcore.ConnectError(str(e)) from e
        self._endpoint._event("connected", send)
        stream = _Stream(sock, self._endpoint)
        try:
            for option in socket_options or []:
                sock.setsockopt(*option)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            stream._use()  # a send cancelled meanwhile writes nothing
        except BaseException:
            stream.close()
            raise
        return stream

    def connect_unix_socket(self, path, timeout=None, socket_options=None):  # pragma: no cover
        raise httpcore.ConnectError("unix sockets are not used")

    def sleep(self, seconds: float) -> None:  # pragma: no cover - retries are not used
        time.sleep(seconds)


class _Stream(httpcore.NetworkStream):
    """A connection's socket; the send whose thread reads or writes it can shut it down."""

    def __init__(self, sock: socket.socket, endpoint: Endpoint):
        self._sock = sock
        self._endpoint = endpoint
        self._lock = threading.Lock()  # guards ``_owner``
        self._owner: Send | None = None  # the send whose exchange uses the connection
        self._closed = False

    def _use(self) -> None:
        """The current thread's send is using the connection: it owns it now."""
        send = getattr(_current, "send", None)
        if send is None:
            return
        with self._lock:
            self._owner = send
        send._attach(self)

    def disown(self, send: Send) -> None:
        """``send``'s exchange is over: the connection belongs to no send until the next uses it."""
        with self._lock:
            if self._owner is send:
                self._owner = None

    def shutdown_for(self, send: Send) -> None:
        """Shut the connection down if ``send`` is still using it (see ``shutdown``)."""
        with self._lock:
            if self._owner is send and send.exchanging:
                self.shutdown()

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self._use()
        try:
            self._sock.settimeout(timeout)
            return self._sock.recv(max_bytes)
        except socket.timeout as e:
            raise httpcore.ReadTimeout(str(e)) from e
        except OSError as e:
            raise httpcore.ReadError(str(e)) from e

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if not buffer:
            return
        self._use()
        try:
            while buffer:
                self._sock.settimeout(timeout)
                buffer = buffer[self._sock.send(buffer) :]
        except socket.timeout as e:
            raise httpcore.WriteTimeout(str(e)) from e
        except OSError as e:
            raise httpcore.WriteError(str(e)) from e

    def shutdown(self) -> None:
        """Wake the thread blocked on this connection: it then fails and closes it."""
        try:
            # The plain socket's shutdown, also for a TLS socket, whose own
            # would tear down its TLS state under the reading thread.
            _SOCKET_SHUTDOWN(self._sock, socket.SHUT_RDWR)
        except OSError:
            pass  # already closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._sock.close()
        with self._lock:
            owner = self._owner
        self._endpoint._event("connection_closed", owner)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._use()
        try:
            self._sock.settimeout(timeout)
            self._sock = ssl_context.wrap_socket(self._sock, server_hostname=server_hostname)
        except socket.timeout as e:
            self.close()
            raise httpcore.ConnectTimeout(str(e)) from e
        except OSError as e:
            self.close()
            raise httpcore.ConnectError(str(e)) from e
        return self

    def get_extra_info(self, info: str):
        if info == "ssl_object":
            return getattr(self._sock, "_sslobj", None)
        if info == "client_addr":
            return self._sock.getsockname()
        if info == "server_addr":
            return self._sock.getpeername()
        if info == "socket":
            return self._sock
        if info == "is_readable":
            if self._sock.fileno() < 0:
                return True
            readable, _, _ = select.select([self._sock], [], [], 0)
            return bool(readable)
        return None

