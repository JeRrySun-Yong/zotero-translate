"""Cancellable single sends and one budget per endpoint (M3 ticket 13; spec section 7, seam 2).

A send can be cancelled while it connects, while it waits for the response
headers and while it reads the body. Cancelling one send leaves the other
sends on the endpoint and the shared HTTP client alone. A send's
concurrency slot is given back only after its local connection has closed,
and the in-flight count includes the connections that have not; the
request timeout follows the same rule.

How each stage is checked (seam 2):

- connecting: a controllable connector blocks the connect; the cancel
  returns at once, nothing is sent once the connect ends, and the slot comes
  back after that (the fake model may never see a connection, so it is not
  asked to see a disconnect);
- waiting for headers and reading the body: the fake model sees the client
  disconnect, and another send on the same endpoint is answered;
- the order of "local connection closed" and "slot given back" comes from
  the events the endpoint reports, not from when the fake model notices.

Two roles with the same address and key share one budget, sized by flash's
concurrency setting; the measured in-flight count stays within it.
"""

import functools
import json
import logging
import socket
import threading
import time
from pathlib import Path

import httpcore
import pytest

from tests.conftest import FAKE_API_KEY
from tests.conftest import FAKE_ARGON_API_KEY
from tests.conftest import FAKE_MODEL_NAME
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.conftest import wait_until
from tests.conftest import write_env_file
from tests.fake_model import FakeModel
from zotero_translate.endpoint import Endpoint
from zotero_translate.endpoint import _Stream  # its close and TLS set-up contract
from zotero_translate.endpoint import SendCancelled
from zotero_translate.endpoint import connect
from zotero_translate.model_client import ModelClient
from zotero_translate.model_client import PipelineModels
from zotero_translate.request_log import REQUESTS_FILE
from zotero_translate.request_log import RequestLog
from zotero_translate.request_log import load_records
from zotero_translate.settings import ModelSettings
from zotero_translate.settings import PipelineSettings

PATH = "/chat/completions"


def _settings(fake: FakeModel, **changes) -> ModelSettings:
    values = dict(
        base_url=fake.base_url,
        api_key=FAKE_API_KEY,
        model="fake-flash",
        concurrency=4,
        timeout_seconds=30.0,
        retry_backoff_seconds=0.01,
        connection_retry_seconds=(0.01, 0.01, 0.01),
        cache_dir=None,
    )
    values.update(changes)
    return ModelSettings(**values)


def _body(text: str) -> dict:
    return {"model": "fake-flash", "messages": [{"role": "user", "content": text}]}


def _content(response: tuple[int, bytes]) -> str:
    status, data = response
    assert status == 200
    return json.loads(data)["choices"][0]["message"]["content"]


class Events:
    """The endpoint's events, each with the endpoint's in-flight count as it happened."""

    def __init__(self):
        self.endpoint: Endpoint | None = None
        self.seen: list[tuple[str, object, int]] = []
        self._lock = threading.Lock()

    def __call__(self, name: str, send) -> None:
        with self._lock:
            self.seen.append((name, send, self.endpoint.in_flight))

    def of(self, send) -> list[tuple[str, int]]:
        with self._lock:
            return [(name, in_flight) for name, s, in_flight in self.seen if s is send]


def wait_for_event(events: Events, name: str, send, timeout: float) -> bool:
    """True if ``send`` gets the event ``name`` within ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if name in [n for n, _ in events.of(send)]:
            return True
        time.sleep(0.01)
    return False


def _endpoint(settings: ModelSettings, **kwargs) -> tuple[Endpoint, Events]:
    events = Events()
    endpoint = Endpoint(settings, on_event=events, **kwargs)
    events.endpoint = endpoint
    return endpoint, events


def _slow_ok(body: dict) -> str:
    time.sleep(0.5)
    return "ok"


@pytest.mark.parametrize(
    "fault", ["timeout", "hang_after_headers"], ids=["waiting_for_headers", "reading_the_body"]
)
def test_a_cancelled_send_disconnects_and_the_other_sends_on_the_endpoint_go_on(fault):
    with FakeModel() as model:
        model.inject_error(fault, kind="other", nth=1)
        model.respond("other", _slow_ok)
        endpoint, events = _endpoint(_settings(model))
        try:
            hung = endpoint.start(PATH, _body("hung"))
            wait_until(lambda: model.in_flight == 1)
            time.sleep(0.2)  # the headers, if any, have reached the client
            other = endpoint.start(PATH, _body("other"))
            wait_until(lambda: model.in_flight == 2)

            hung.cancel()

            assert hung.wait(2), "the cancelled send did not end"
            with pytest.raises(SendCancelled):
                hung.result()
            # The endpoint saw the client go, and the other send is answered.
            wait_until(lambda: model.requests[0].disconnected, timeout=2)
            assert other.wait(5) and _content(other.result()) == "ok"
            # The shared client is still open: a new send on it is answered.
            again = endpoint.start(PATH, _body("again"))
            assert again.wait(5) and _content(again.result()) == "ok"
            assert endpoint.in_flight == 0
        finally:
            endpoint.close()

    # The connection closed first, while the send still counted as in
    # flight; then its slot was given back.
    names = [name for name, _ in events.of(hung)]
    assert names.index("connection_closed") < names.index("slot_released")
    in_flight = dict(events.of(hung))
    assert in_flight["connection_closed"] >= 1


def test_cancelling_a_send_whose_connection_went_back_to_the_pool_spares_the_next_send_on_it():
    # A's whole response is read and its connection goes back to the pool;
    # B takes that connection. A cancel of A now (say its timeout fires)
    # must not touch B's request, and the connection's later close belongs
    # to no send that has already given its slot back.
    with FakeModel(keep_alive=True) as model:
        model.respond("other", _slow_ok)
        events = Events()
        sends: dict[str, object] = {}

        def on_event(name: str, send) -> None:
            events(name, send)
            if name == "connection_returned" and send is sends.get("a"):
                sends["b"] = endpoint.start(PATH, _body("b"))
                wait_until(lambda: model.in_flight == 1)  # B is on the reused connection
                send.cancel()

        endpoint = Endpoint(_settings(model), on_event=on_event)
        events.endpoint = endpoint
        try:
            sends["a"] = endpoint.start(PATH, _body("a"))
            assert sends["a"].wait(10)
            assert _content(sends["a"].result()) == "ok"
            assert sends["b"].wait(10)
            assert _content(sends["b"].result()) == "ok"
        finally:
            endpoint.close()

    a, b = model.requests
    assert a.client_port == b.client_port  # one connection, reused
    assert not b.disconnected
    for send in sends.values():
        names = [name for name, _ in events.of(send)]
        assert names[-1] == "slot_released", names  # nothing of the send after its slot is back


@pytest.mark.parametrize("window", ["response_read", "connection_returned"])
def test_a_cancel_after_the_whole_response_never_reaches_the_next_send_on_the_connection(window):
    # A has read its whole response; in that window (before or after its
    # connection went back to the pool) B starts and takes a connection,
    # and A is cancelled just before B claims it. B must be untouched.
    with FakeModel(keep_alive=True) as model:
        events = Events()
        sends: dict[str, object] = {}

        def on_event(name: str, send) -> None:
            events(name, send)
            a = sends.get("a")
            if name == window and send is a and "b" not in sends:
                sends["b"] = endpoint.start(PATH, _body("b"))
                assert sends["b"].wait(10)  # B runs to its end inside A's window
            elif name == "connection_taken" and a is not None and send is not a:
                a.cancel()  # B has the connection and has not claimed it yet

        endpoint = Endpoint(_settings(model), on_event=on_event)
        events.endpoint = endpoint
        try:
            sends["a"] = endpoint.start(PATH, _body("a"))
            assert sends["a"].wait(15)
            assert _content(sends["a"].result()) == "ok"  # its whole response was read
            assert _content(sends["b"].result()) == "ok"
        finally:
            endpoint.close()

    a, b = model.requests
    assert not b.disconnected
    if window == "connection_returned":
        assert a.client_port == b.client_port  # B did reuse A's connection


def test_a_send_cancelled_during_the_tls_handshake_ends_at_once():
    # A TCP server that takes the connection and never answers the
    # ClientHello: the send hangs in the TLS handshake.
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    received = threading.Event()
    disconnected = threading.Event()

    def serve() -> None:
        connection, _ = server.accept()
        with connection:
            while True:
                data = connection.recv(4096)
                if not data:
                    disconnected.set()
                    return
                received.set()

    threading.Thread(target=serve, daemon=True).start()
    settings = ModelSettings(base_url=f"https://127.0.0.1:{port}/v1", api_key="k", concurrency=1)
    endpoint, events = _endpoint(settings)
    try:
        send = endpoint.start(PATH, _body("tls"))
        assert received.wait(5)  # the ClientHello arrived
        time.sleep(0.2)
        assert endpoint.in_flight == 1

        started = time.monotonic()
        send.cancel()
        assert send.wait(2), "the cancel did not reach the TLS handshake"
        assert time.monotonic() - started < 1
        with pytest.raises(SendCancelled):
            send.result()
        assert endpoint.in_flight == 0
        assert disconnected.wait(2)  # the server saw the client go
    finally:
        endpoint.close()
        server.close()

    names = [name for name, _ in events.of(send)]
    assert names.index("connection_closed") < names.index("slot_released")


class _HeldServer:
    """A TCP server that holds each connection it accepts until told to drop it.

    ``answer`` connections get a whole HTTP answer once their request has
    arrived; the others get nothing.
    """

    def __init__(self, answer: bool = False):
        self.answer = answer
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self.received = threading.Event()  # some bytes of the first connection arrived
        self.disconnected = threading.Event()  # the first connection's client went
        # End the first connection from the server's side: the client reads
        # its end; the server's own descriptor stays open meanwhile.
        self.drop = threading.Event()
        self._first = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            first, self._first = self._first, False
            threading.Thread(target=self._hold, args=(connection, first), daemon=True).start()

    def _hold(self, connection: socket.socket, first: bool) -> None:
        with connection:
            connection.settimeout(0.05)
            dropped = False
            while True:
                if first and self.drop.is_set() and not dropped:
                    connection.shutdown(socket.SHUT_WR)
                    dropped = True
                try:
                    data = connection.recv(65536)
                except TimeoutError:
                    continue
                except OSError:
                    data = b""
                if not data:
                    if first:
                        self.disconnected.set()
                    return
                if first:
                    self.received.set()
                elif self.answer and b"\r\n\r\n" in data:
                    payload = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
                    connection.sendall(
                        b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                        + payload
                    )
                    return

    def close(self) -> None:
        self.server.close()


def test_a_cancel_about_to_shut_a_connection_down_and_its_close_never_overlap():
    # A's cancel has checked that the connection is A's and is about to
    # shut it down; meanwhile A's thread loses the connection (the server
    # drops it) and closes it. Were the close allowed to finish then, the
    # descriptor could go to B's new connection before the shutdown runs.
    held = _HeldServer(answer=True)
    descriptors: list[int] = []

    def recording_connector(host, port, timeout, cancelled):
        sock = connect(host, port, timeout, cancelled)
        descriptors.append(sock.fileno())
        return sock

    paused = threading.Event()
    resume = threading.Event()
    events = Events()

    def on_event(name: str, send) -> None:
        events(name, send)
        if name == "cancel_reaching":
            paused.set()
            resume.wait(5)

    settings = ModelSettings(base_url=f"http://127.0.0.1:{held.port}/v1", api_key="k", concurrency=4)
    endpoint = Endpoint(settings, connector=recording_connector, on_event=on_event)
    events.endpoint = endpoint
    try:
        a = endpoint.start(PATH, _body("a"))
        assert held.received.wait(5)
        time.sleep(0.3)  # the whole request is out: A's thread waits in its read
        canceller = threading.Thread(target=a.cancel)
        canceller.start()
        assert paused.wait(5)  # the cancel is between its check and the shutdown

        held.drop.set()  # A's thread now reads the end of the connection and closes it
        time.sleep(0.5)
        # B opens a new connection now; it would get A's descriptor if A's
        # socket had been closed under the paused cancel.
        b = endpoint.start(PATH, _body("b"))
        assert b.wait(5)
        resume.set()
        canceller.join(5)
        assert a.wait(5)
        assert descriptors[1] != descriptors[0], (
            f"A's socket closed while its cancel was about to shut it down: B's new "
            f"connection got A's descriptor {descriptors[0]}"
        )
        assert _content(b.result()) == "ok"  # B's connection is untouched
    finally:
        resume.set()
        endpoint.close()
        held.close()


def test_a_send_cancelled_just_before_its_tls_handshake_closes_its_connection_first():
    held = _HeldServer()
    events = Events()

    def on_event(name: str, send) -> None:
        events(name, send)
        if name == "tls_handshake":
            send.cancel()  # after the TCP connect, before the handshake

    settings = ModelSettings(base_url=f"https://127.0.0.1:{held.port}/v1", api_key="k", concurrency=1)
    endpoint = Endpoint(settings, on_event=on_event)
    events.endpoint = endpoint
    try:
        send = endpoint.start(PATH, _body("tls"))
        assert send.wait(5)
        with pytest.raises(SendCancelled):
            send.result()
        assert endpoint.in_flight == 0
        assert held.disconnected.wait(2)  # the server saw the client go
        assert not held.received.is_set()  # not even a ClientHello went out
    finally:
        endpoint.close()
        held.close()

    names = [name for name, _ in events.of(send)]
    assert "connection_closed" in names, names
    assert names.index("connection_closed") < names.index("slot_released")


class _DetachingContext:
    """Stands in for an SSL context: like ``wrap_socket``, it detaches the socket
    it is given and returns a new socket object that owns the descriptor."""

    def wrap_socket(self, sock: socket.socket, server_hostname=None) -> socket.socket:
        return socket.socket(fileno=sock.detach())


def test_a_connection_closed_while_its_tls_socket_is_set_up_does_not_leave_it_open():
    # E.g. a proxy tunnel's connection closed as the endpoint closes, just
    # after the handshake and before the TLS socket replaces the raw one.
    ours, theirs = socket.socketpair()
    theirs.settimeout(2)
    streams: list = []

    def on_event(name: str, send) -> None:
        if name == "tls_established":
            streams[0].close()

    with FakeModel() as model:
        endpoint = Endpoint(_settings(model), on_event=on_event)
        try:
            streams.append(_Stream(ours, endpoint))
            with pytest.raises(httpcore.ConnectError):
                streams[0].start_tls(_DetachingContext(), server_hostname="x")
            assert theirs.recv(1) == b""  # our end is closed
        finally:
            endpoint.close()
            theirs.close()


def test_a_send_cancelled_while_connecting_ends_sends_nothing_and_gives_its_slot_back():
    entered = threading.Event()
    release = threading.Event()

    def connector(host: str, port: int, timeout: float | None, cancelled: threading.Event):
        entered.set()
        release.wait(10)  # a connect that does not look at the cancel
        return socket.create_connection((host, port), timeout)

    with FakeModel() as model:
        endpoint, events = _endpoint(_settings(model, concurrency=1), connector=connector)
        try:
            send = endpoint.start(PATH, _body("connecting"))
            assert entered.wait(5)

            started = time.monotonic()
            send.cancel()
            assert time.monotonic() - started < 0.5  # the cancel itself completes at once

            # The connect has not ended, so the send still holds its slot.
            assert not send.wait(0.3)
            assert endpoint.in_flight == 1
            release.set()
            assert send.wait(5)
            with pytest.raises(SendCancelled):
                send.result()
            assert endpoint.in_flight == 0

            # The slot is back: the next send (concurrency 1) goes out and is answered.
            again = endpoint.start(PATH, _body("again"))
            assert again.wait(5) and _content(again.result()) == "ok"
        finally:
            endpoint.close()

    # Nothing of the cancelled send reached the endpoint.
    assert [r.body["messages"][0]["content"] for r in model.requests] == ["again"]
    names = [name for name, _ in events.of(send)]
    assert names == ["connected", "connection_taken", "connection_closed", "slot_released"]


def test_the_default_connect_gives_up_when_its_send_is_cancelled():
    # The production connector, with its wait for the connect to finish
    # replaced by one that never sees an answer (a SYN nobody answers).
    sockets: list[socket.socket] = []

    def unanswered(sock: socket.socket, seconds: float) -> bool:
        sockets.append(sock)
        time.sleep(seconds)
        return False

    connector = functools.partial(connect, wait_writable=unanswered)
    with FakeModel() as model:
        endpoint, events = _endpoint(_settings(model, concurrency=1), connector=connector)
        try:
            send = endpoint.start(PATH, _body("connecting"))
            wait_until(lambda: sockets)
            time.sleep(0.2)  # several polls of the cancel flag go by
            assert endpoint.in_flight == 1

            started = time.monotonic()
            send.cancel()
            assert send.wait(2)
            assert time.monotonic() - started < 0.5  # the next poll sees the cancel
            with pytest.raises(SendCancelled):
                send.result()
            assert endpoint.in_flight == 0
            assert sockets[0].fileno() == -1  # the half-open socket is closed
        finally:
            endpoint.close()

    assert model.requests == []
    assert [name for name, _ in events.of(send)] == ["slot_released"]  # it never connected


def test_a_timed_out_send_gives_its_slot_back_only_after_its_connection_closed(tmp_path):
    with FakeModel() as model:
        model.fail_same_request(["timeout"])
        settings = _settings(model, concurrency=1, timeout_seconds=0.5)
        endpoint, events = _endpoint(settings)
        log = RequestLog(tmp_path / REQUESTS_FILE)
        client = ModelClient(settings, role="flash", request_log=log, endpoint=endpoint)
        try:
            assert client.complete([{"role": "user", "content": "hello"}]) == "ok"
        finally:
            client.close()
        timed_out = model.requests[0]
        wait_until(lambda: timed_out.disconnected, timeout=2)

    assert [r["result"] for r in load_records(log.path)] == ["timeout", "succeeded"]
    sends = {send for _, send, _ in events.seen if send is not None}  # None: a connection no send used
    first, second = sorted(sends, key=lambda s: s.sent)
    names = [name for name, _ in events.of(first)]
    assert names.index("connection_closed") < names.index("slot_released")
    # The resend (concurrency 1) could only go out once the slot was back.
    order = [(name, send) for name, send, _ in events.seen]
    assert order.index(("slot_released", first)) < order.index(("connected", second))
    assert endpoint.in_flight == 0


def test_closing_the_client_aborts_its_sends_in_flight_at_once(tmp_path):
    # A paper task that fails closes its clients; a send still in flight
    # (e.g. an algorithm's) must end then, not at the request timeout.
    with FakeModel() as model:
        model.inject_error("timeout")
        log = RequestLog(tmp_path / REQUESTS_FILE)
        client = ModelClient(_settings(model, timeout_seconds=60), role="flash", request_log=log)
        raised: list[BaseException] = []

        def complete():
            try:
                client.complete([{"role": "user", "content": "hello"}], kind="algorithm_translation")
            except BaseException as e:  # noqa: BLE001 - the test looks at it
                raised.append(e)

        thread = threading.Thread(target=complete)
        thread.start()
        wait_until(lambda: model.in_flight == 1)
        started = time.monotonic()
        client.close()
        thread.join(5)
        assert not thread.is_alive()
        assert time.monotonic() - started < 3
        wait_until(lambda: model.requests[0].disconnected, timeout=2)

    assert len(raised) == 1 and isinstance(raised[0], SendCancelled)
    assert len(model.requests) == 1  # no resend
    (record,) = load_records(log.path)
    assert (record["send"], record["result"]) == (1, "error")
    assert record["seconds"] < 3


def _complete_many(models: PipelineModels, count: int) -> None:
    clients = [models.flash, models.argon]
    threads = [
        threading.Thread(
            target=clients[n % 2].complete, args=([{"role": "user", "content": f"request {n}"}],)
        )
        for n in range(count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)


def test_two_roles_on_the_same_endpoint_share_one_budget_sized_by_flash(caplog):
    with FakeModel(delay=0.3) as model:
        settings = PipelineSettings(
            flash=_settings(model, concurrency=2),
            argon=_settings(model, model="fake-argon", concurrency=16),  # same address and key
        )
        with caplog.at_level(logging.INFO, logger="zotero_translate.model_client"):
            models = PipelineModels(settings)
        try:
            _complete_many(models, 12)
        finally:
            models.close()

    assert len(model.requests) == 12
    assert {r.body["model"] for r in model.requests} == {"fake-flash", "fake-argon"}
    assert model.max_in_flight == 2
    assert models.flash.endpoint is models.argon.endpoint
    assert models.argon.concurrency == models.flash.concurrency == 2
    assert models.flash.endpoint.max_in_flight == 2
    [line] = [r.getMessage() for r in caplog.records if "share" in r.getMessage()]
    assert "16" in line and "ignored" in line
    assert FAKE_API_KEY not in caplog.text


def test_addresses_that_differ_only_in_case_or_a_trailing_slash_are_the_same_endpoint():
    with FakeModel() as model:
        upper = model.base_url.replace("http://", "HTTP://") + "/"
        settings = PipelineSettings(
            flash=_settings(model, concurrency=2),
            argon=_settings(model, base_url=upper, model="fake-argon", concurrency=16),
        )
        models = PipelineModels(settings)
        try:
            assert models.argon.complete([{"role": "user", "content": "hello"}]) == "ok"
        finally:
            models.close()

    assert models.flash.endpoint is models.argon.endpoint
    assert models.argon.concurrency == 2


@pytest.mark.parametrize(
    "flash_path, argon_path, shared",
    [("/a/b", "/a%2Fb", False), ("/a/b", "/A/b", False), ("/v1", "/v1/", True)],
)
def test_the_paths_of_two_addresses_are_compared_as_written(flash_path, argon_path, shared):
    with FakeModel() as model:
        root = model.base_url.removesuffix("/v1")
        settings = PipelineSettings(
            flash=_settings(model, base_url=root + flash_path, concurrency=2),
            argon=_settings(model, base_url=root + argon_path, concurrency=16),
        )
        models = PipelineModels(settings)
        models.close()

    assert (models.flash.endpoint is models.argon.endpoint) is shared


def test_two_roles_with_different_keys_keep_their_own_budgets(caplog):
    with FakeModel(delay=0.3) as model:
        settings = PipelineSettings(
            flash=_settings(model, concurrency=2),
            argon=_settings(model, model="fake-argon", concurrency=2, api_key=FAKE_ARGON_API_KEY),
        )
        with caplog.at_level(logging.INFO, logger="zotero_translate.model_client"):
            models = PipelineModels(settings)
        try:
            _complete_many(models, 12)
        finally:
            models.close()

    assert models.flash.endpoint is not models.argon.endpoint
    assert model.max_in_flight == 4
    assert not [r for r in caplog.records if "share" in r.getMessage()]


def test_a_task_whose_roles_share_an_endpoint_stays_within_the_shared_budget(tmp_path):
    with FakeModel(delay=0.05) as model:
        # Argon's file names the same address and key as flash's, and its
        # own limit of 16, which the shared budget ignores.
        argon_env = write_env_file(tmp_path, model.base_url, FAKE_API_KEY, None, None, 16)
        service = launch_service(model, tmp_path, concurrency=2, argon_env_file=argon_env)
        try:
            task_id = service.submit()
            final = service.wait(task_id)
            report = service.report(task_id)
        finally:
            stop_service(service)

    assert final["state"] == "succeeded", final["error"]
    assert {r.body["model"] for r in model.requests} >= {FAKE_MODEL_NAME}
    assert len(model.requests) > 2
    assert model.max_in_flight <= 2
    endpoints = report["model"]["endpoints"]
    assert endpoints["flash"]["concurrency"] == endpoints["argon"]["concurrency"] == 2
    shared = report["model"]["shared_budget"]
    assert shared["concurrency"] == 2
    assert shared["ignored_argon_concurrency"] == 16
    assert 1 <= shared["max_in_flight"] <= 2
