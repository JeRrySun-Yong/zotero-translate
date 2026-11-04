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
    assert names == ["connected", "connection_closed", "slot_released"]


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
    first, second = sorted({send for _, send, _ in events.seen}, key=lambda s: s.sent)
    names = [name for name, _ in events.of(first)]
    assert names.index("connection_closed") < names.index("slot_released")
    # The resend (concurrency 1) could only go out once the slot was back.
    order = [(name, send) for name, send, _ in events.seen]
    assert order.index(("slot_released", first)) < order.index(("connected", second))
    assert client._in_flight == 0 and endpoint.in_flight == 0


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
            task_log = (service.data_dir / "tasks" / task_id / "task.log").read_text("utf-8")
        finally:
            stop_service(service)

    assert final["state"] == "succeeded", final["error"]
    assert {r.body["model"] for r in model.requests} >= {FAKE_MODEL_NAME}
    assert len(model.requests) > 2
    assert model.max_in_flight <= 2
    endpoints = report["model"]["endpoints"]
    assert endpoints["flash"]["concurrency"] == endpoints["argon"]["concurrency"] == 2
    assert endpoints["flash"]["shared_budget"] is endpoints["argon"]["shared_budget"] is True
    assert endpoints["flash"]["budget_max_in_flight"] <= 2
    assert "ignored" in task_log and FAKE_API_KEY not in task_log
