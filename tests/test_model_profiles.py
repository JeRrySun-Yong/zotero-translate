"""Model profiles through the translation service's HTTP interface (M4 ticket 02).

The service lists the built-in profile "Gemini" (its start options) and the
user profiles saved in its profiles directory, never with their API keys;
creates, edits and deletes user profiles; tests a profile's connection with
one small request; and applies a profile that passed its test as the current
one, which survives a restart. The write requests need the custom request
header and a JSON body, so a web page cannot send them. No paper task runs
here: the tasks that use a profile are in ``test_model_profile_tasks.py``.
"""

import json
import os
import socket
import stat

import httpx
import pytest

from tests.conftest import EXCERPT
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FakeModel

HEADERS = {"X-Zotero-Translate": "1"}
KEY = "sk-profile-secret-key-4321"
OTHER_KEY = "sk-profile-other-key-8765"
PROFILE_MODEL = "fake-deepseek-v4.1-flash"
CURRENT_RESET = "event=当前配置改回 Gemini"


def _write(service, method: str, path: str, body: dict | None = None) -> httpx.Response:
    return service.http.request(method, path, json=body if body is not None else {}, headers=HEADERS)


def _create(service, model: FakeModel | str, **fields) -> dict:
    base_url = model if isinstance(model, str) else model.base_url
    body = {"name": "DeepSeek", "base_url": base_url, "api_key": KEY, "model": PROFILE_MODEL, **fields}
    response = _write(service, "POST", "/profiles", body)
    assert response.status_code == 201, response.text
    return response.json()


def _profiles(service) -> dict:
    response = service.http.get("/profiles")
    assert response.status_code == 200, response.text
    return response.json()


def _listed(service, profile_id: str) -> dict:
    [profile] = [p for p in _profiles(service)["profiles"] if p["id"] == profile_id]
    return profile


def _closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def test_a_new_profile_is_listed_without_its_key(start_service):
    with FakeModel() as gemini, FakeModel() as deepseek:
        service = start_service(gemini)
        created = _create(service, deepseek, concurrency=8)
        listing = service.http.get("/profiles")

    assert listing.status_code == 200
    profiles = listing.json()
    assert profiles["current"] == "gemini"
    builtin, profile = profiles["profiles"]
    assert builtin["id"] == "gemini" and builtin["builtin"] and builtin["name"] == "Gemini"
    assert builtin["current"] and builtin["tested"]
    assert profile == created
    assert profile["name"] == "DeepSeek"
    assert profile["base_url"] == deepseek.base_url
    assert profile["model"] == PROFILE_MODEL
    assert profile["concurrency"] == 8
    assert profile["request_timeout_seconds"] is None
    assert profile["api_key"] == {"set": True, "last4": "4321"}
    assert not profile["builtin"] and not profile["current"] and not profile["tested"]
    assert profile["last_test"] is None
    # The key is in the profile's file, readable by its owner alone; nowhere else.
    path = service.profiles_dir / f"{profile['id']}.env"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert KEY not in json.dumps(created) and KEY not in listing.text
    assert KEY not in "\n".join(service.output)
    assert any(f"profile={profile['id']} event=新建配置" in line for line in service.output)


def test_a_profile_takes_the_default_concurrency_of_16(start_service):
    with FakeModel() as model:
        service = start_service(model)
        assert _create(service, model)["concurrency"] == 16


@pytest.mark.parametrize(
    "fields",
    [
        {"name": ""},
        {"base_url": "ftp://example.com/v1"},
        {"api_key": ""},
        {"model": ""},
        {"concurrency": 0},
        {"concurrency": "many"},
        {"request_timeout_seconds": -1},
        {"name": "two\nlines"},
    ],
    ids=["no-name", "not-http", "no-key", "no-model", "zero-concurrency", "text-concurrency",
         "negative-timeout", "newline"],
)
def test_a_profile_with_a_bad_field_is_refused(start_service, fields):
    with FakeModel() as model:
        service = start_service(model)
        body = {"name": "DeepSeek", "base_url": model.base_url, "api_key": KEY, "model": PROFILE_MODEL, **fields}
        response = _write(service, "POST", "/profiles", body)
        assert response.status_code == 400, response.text
        assert response.json()["error"]
        assert len(_profiles(service)["profiles"]) == 1


def test_an_edit_keeps_the_key_unless_given_and_untests_the_profile(start_service):
    with FakeModel() as model:
        service = start_service(model)
        profile = _create(service, model)
        assert _write(service, "POST", f"/profiles/{profile['id']}/test").json()["ok"]
        renamed = _write(service, "PUT", f"/profiles/{profile['id']}", {"name": "DeepSeek V4.1"})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "DeepSeek V4.1"
        assert renamed.json()["api_key"] == {"set": True, "last4": "4321"}
        assert renamed.json()["tested"]  # a new name changes no connection setting
        rekeyed = _write(service, "PUT", f"/profiles/{profile['id']}", {"api_key": OTHER_KEY})
        assert rekeyed.status_code == 200, rekeyed.text
        assert rekeyed.json()["api_key"] == {"set": True, "last4": "8765"}
        assert not rekeyed.json()["tested"]
        assert OTHER_KEY not in rekeyed.text and OTHER_KEY not in "\n".join(service.output)
        assert _write(service, "PUT", "/profiles/0123456789ab", {"name": "x"}).status_code == 404


# 测试连接对 成功 / 连不上 / 401 / 404 / 超时 / 回答不能解析 各给出对应结果.
@pytest.mark.parametrize(
    "case, expected",
    [
        ("succeeded", "succeeded"),
        ("unreachable", "unreachable"),
        ("401", "unauthorized"),
        ("403", "unauthorized"),
        ("404", "model_not_found"),
        ("timeout", "timeout"),
        ("unparseable", "unparseable"),
    ],
)
def test_the_connection_test_tells_each_outcome_apart(start_service, case, expected):
    with FakeModel() as gemini, FakeModel() as deepseek:
        service = start_service(gemini)
        if case in ("401", "403", "404", "timeout"):
            deepseek.inject_error(case, kind="connection_test")
        if case == "unparseable":
            deepseek.respond("connection_test", lambda body: "Sure! The connection works.")
        url = _closed_port_url() if case == "unreachable" else deepseek
        profile = _create(service, url, request_timeout_seconds=2)
        response = _write(service, "POST", f"/profiles/{profile['id']}/test")
        listed = _listed(service, profile["id"])

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["result"] == expected
    assert result["ok"] is (expected == "succeeded")
    assert result["message"]
    assert isinstance(result["seconds"], (int, float))
    assert listed["tested"] is (expected == "succeeded")
    assert listed["last_test"]["result"] == expected
    if case != "unreachable":
        # One request, with the paper task's parameters.
        [request] = deepseek.requests
        assert request.body["model"] == PROFILE_MODEL
        assert request.body["reasoning_effort"] == "high"
        assert request.body["temperature"] == 1.0
        assert request.headers["Authorization"] == f"Bearer {KEY}"
    if case in ("401", "403", "404"):
        assert result["http_status"] == int(case)
    assert KEY not in response.text and KEY not in "\n".join(service.output)
    assert any(f"profile={profile['id']} event=测试连接" in line for line in service.output)


def test_the_answer_is_read_from_content_only(start_service):
    """DeepSeek on Command Code puts its thinking in ``reasoning_content`` or ``reasoning``."""

    def thinking_answer(body):
        return '```json\n{"ok": true}\n```'

    with FakeModel() as model:
        model.respond("connection_test", thinking_answer)
        service = start_service(model)
        profile = _create(service, model)
        result = _write(service, "POST", f"/profiles/{profile['id']}/test").json()
    assert result["result"] == "succeeded", result


def test_only_a_profile_that_passed_its_test_can_be_applied(start_service):
    with FakeModel() as gemini, FakeModel() as deepseek:
        service = start_service(gemini)
        profile = _create(service, deepseek)
        path = f"/profiles/{profile['id']}"

        untested = _write(service, "POST", f"{path}/apply")
        assert untested.status_code == 409, untested.text
        deepseek.inject_error("401", kind="connection_test")
        assert _write(service, "POST", f"{path}/test").json()["result"] == "unauthorized"
        failed = _write(service, "POST", f"{path}/apply")
        assert failed.status_code == 409, failed.text
        assert _profiles(service)["current"] == "gemini"

        assert _write(service, "POST", f"{path}/test").json()["ok"]
        applied = _write(service, "POST", f"{path}/apply")
        assert applied.status_code == 200, applied.text
        assert applied.json()["current"] == profile["id"]
        assert _profiles(service)["current"] == profile["id"]
        assert _listed(service, profile["id"])["current"]
        assert any(f"profile={profile['id']} event=应用配置" in line for line in service.output)

        # A new key is untested: the current profile goes back to Gemini.
        assert _write(service, "PUT", path, {"api_key": OTHER_KEY}).status_code == 200
        assert _profiles(service)["current"] == "gemini"
        assert sum(CURRENT_RESET in line for line in service.output) == 1

        # Gemini needs no test.
        assert _write(service, "POST", "/profiles/gemini/apply").status_code == 200
        assert _write(service, "POST", "/profiles/0123456789ab/apply").status_code == 404


def test_write_requests_without_the_header_or_a_json_body_are_refused(start_service):
    with FakeModel() as model:
        service = start_service(model)
        profile = _create(service, model)
        assert _write(service, "POST", f"/profiles/{profile['id']}/test").json()["ok"]
        body = {"name": "Evil", "base_url": "http://evil.example/v1", "api_key": "k", "model": "m"}
        refused = [
            # No custom header: what a cross-origin form or fetch without preflight sends.
            service.http.post("/profiles", json=body),
            service.http.post(f"/profiles/{profile['id']}/apply", json={}),
            service.http.request("DELETE", f"/profiles/{profile['id']}", json={}),
            service.http.put(f"/profiles/{profile['id']}", json={"base_url": "http://evil.example/v1"}),
            # The header, but not a JSON body.
            service.http.post("/profiles", data=body, headers=HEADERS),
            service.http.post(
                "/profiles", content=json.dumps(body), headers={**HEADERS, "Content-Type": "text/plain"}
            ),
            service.http.post(
                "/profiles", content="{not json", headers={**HEADERS, "Content-Type": "application/json"}
            ),
            service.http.post(
                "/profiles", content="[1, 2]", headers={**HEADERS, "Content-Type": "application/json"}
            ),
            # The header and JSON, from a page whose name resolves to this machine.
            service.http.post("/profiles", json=body, headers={**HEADERS, "Host": "evil.example:8920"}),
        ]
        preflight = service.http.request(
            "OPTIONS",
            "/profiles",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-zotero-translate, content-type",
            },
        )
        profiles = _profiles(service)

    assert [r.status_code for r in refused] == [403, 403, 403, 403, 415, 415, 400, 400, 403]
    assert all(r.json()["error"] for r in refused)
    assert preflight.status_code >= 400
    assert not any(h.lower().startswith("access-control-") for h in preflight.headers)
    assert [p["name"] for p in profiles["profiles"]] == ["Gemini", "DeepSeek"]
    assert profiles["profiles"][1]["base_url"] == model.base_url
    assert profiles["current"] == "gemini"


def test_the_current_profile_survives_a_restart_and_falls_back_when_its_file_is_gone(tmp_path):
    with FakeModel() as model:
        service = launch_service(model, tmp_path)
        try:
            profile = _create(service, model)
            assert _write(service, "POST", f"/profiles/{profile['id']}/test").json()["ok"]
            assert _write(service, "POST", f"/profiles/{profile['id']}/apply").status_code == 200
        finally:
            stop_service(service)
        service = launch_service(model, tmp_path, data_dir=service.data_dir)
        try:
            assert _profiles(service)["current"] == profile["id"]
            assert _listed(service, profile["id"])["tested"]
        finally:
            stop_service(service)

        (service.profiles_dir / f"{profile['id']}.env").unlink()
        service = launch_service(model, tmp_path, data_dir=service.data_dir)
        try:
            profiles = _profiles(service)
        finally:
            stop_service(service)

    assert profiles["current"] == "gemini"
    assert [p["id"] for p in profiles["profiles"]] == ["gemini"]
    [reset] = [line for line in service.output if CURRENT_RESET in line]
    assert f"profile={profile['id']}" in reset and "reason=" in reset


def test_the_service_notices_a_deleted_file_while_it_runs(start_service):
    with FakeModel() as model:
        service = start_service(model)
        profile = _create(service, model)
        assert _write(service, "POST", f"/profiles/{profile['id']}/test").json()["ok"]
        assert _write(service, "POST", f"/profiles/{profile['id']}/apply").status_code == 200
        (service.profiles_dir / f"{profile['id']}.env").unlink()
        assert _profiles(service)["current"] == "gemini"
        assert _profiles(service)["current"] == "gemini"
    assert sum(CURRENT_RESET in line for line in service.output) == 1


def test_deleting_profiles(start_service, tmp_path):
    pause = tmp_path / "pause"
    pause.mkdir()
    with FakeModel() as model:
        service = start_service(model, env={"ZT_TEST_PAUSE_AFTER_MARKER": str(pause)})
        # Gemini is the service's start options: no edit, no delete, no test.
        assert _write(service, "DELETE", "/profiles/gemini").status_code == 409
        assert _write(service, "PUT", "/profiles/gemini", {"name": "x"}).status_code == 409
        assert _write(service, "POST", "/profiles/gemini/test").status_code == 409

        current = _create(service, model)
        assert _write(service, "POST", f"/profiles/{current['id']}/test").json()["ok"]
        assert _write(service, "POST", f"/profiles/{current['id']}/apply").status_code == 200
        # A task submitted now uses it, and holds it while unfinished.
        task_id = service.submit()
        assert service.status(task_id)["profile"] == {"id": current["id"], "name": "DeepSeek"}
        in_use = _write(service, "DELETE", f"/profiles/{current['id']}")
        assert in_use.status_code == 409, in_use.text

        other = _create(service, model, name="DeepSeek Fast")
        deleted = _write(service, "DELETE", f"/profiles/{other['id']}")
        assert deleted.status_code == 200, deleted.text
        assert not (service.profiles_dir / f"{other['id']}.env").exists()
        assert _write(service, "DELETE", f"/profiles/{other['id']}").status_code == 404
        assert any(f"profile={other['id']} event=删除配置" in line for line in service.output)

        cancelled = service.http.post(f"/tasks/{task_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        # The current profile may be deleted once no task uses it: back to Gemini.
        assert _write(service, "DELETE", f"/profiles/{current['id']}").status_code == 200
        assert _profiles(service)["current"] == "gemini"
        assert [p["id"] for p in _profiles(service)["profiles"]] == ["gemini"]


@pytest.mark.parametrize("fault", ["200_echo", "400_echo"])
def test_a_key_the_endpoint_echoes_is_redacted(start_service, fault):
    """An endpoint that echoes the request's Authorization header, in an
    answer or an error, gets no key into the answer, the saved test result
    or the event log."""
    with FakeModel() as gemini, FakeModel() as deepseek:
        deepseek.inject_error(fault, kind="connection_test")
        service = start_service(gemini)
        profile = _create(service, deepseek)
        response = _write(service, "POST", f"/profiles/{profile['id']}/test")
        listing = service.http.get("/profiles").text
        [echoed] = deepseek.requests
        assert echoed.fault == fault

    assert response.status_code == 200, response.text
    assert not response.json()["ok"]
    assert "redacted" in response.json()["message"]  # the echo is there, without the key
    state = (service.data_dir / "model-profiles.json").read_text("utf-8")
    for text in (response.text, listing, state, "\n".join(service.output)):
        assert KEY not in text


def _submit(service, profile: str | None = None) -> httpx.Response:
    data = {"filename": "paper.pdf", **({"profile": profile} if profile is not None else {})}
    return service.http.post(
        "/tasks", files={"file": ("paper.pdf", EXCERPT.read_bytes(), "application/pdf")}, data=data
    )


def test_a_submission_may_name_its_profile(start_service, tmp_path):
    """A retry resubmits the paper with the profile of the failed task, whatever is current."""
    pause = tmp_path / "pause"
    pause.mkdir()
    with FakeModel() as gemini, FakeModel() as deepseek:
        # No task gets past its process marker: no paper runs.
        service = start_service(gemini, env={"ZT_TEST_PAUSE_AFTER_MARKER": str(pause)})
        tested = _create(service, deepseek)
        assert _write(service, "POST", f"/profiles/{tested['id']}/test").json()["ok"]
        untested = _create(service, deepseek, name="DeepSeek Fast")

        named = _submit(service, tested["id"])  # Gemini is current
        assert named.status_code == 201, named.text
        assert service.status(named.json()["task_id"])["profile"] == {"id": tested["id"], "name": "DeepSeek"}

        assert _write(service, "POST", f"/profiles/{tested['id']}/apply").status_code == 200
        gemini_named = _submit(service, "gemini")
        assert service.status(gemini_named.json()["task_id"])["profile"] == {"id": "gemini", "name": "Gemini"}
        current = _submit(service)
        assert service.status(current.json()["task_id"])["profile"]["id"] == tested["id"]

        refused = {
            "untested": _submit(service, untested["id"]),
            "unknown": _submit(service, "0123456789ab"),
            "malformed": _submit(service, "../model"),
        }
        tasks = list((service.data_dir / "tasks").iterdir())

    assert {name: r.status_code for name, r in refused.items()} == {
        "untested": 409,
        "unknown": 422,
        "malformed": 422,
    }
    assert all(r.json()["error"] for r in refused.values())
    assert len(tasks) == 3  # a refused submission leaves no task behind


BAD_ADDRESSES = ["not a url", "ftp://x", "http://", "http:///v1", "http://[::1", "http://host:99999/v1"]


def test_an_invalid_address_is_refused_when_saved(start_service):
    with FakeModel() as model:
        service = start_service(model)
        profile = _create(service, model)
        body = {"name": "DeepSeek", "api_key": KEY, "model": PROFILE_MODEL}
        created = [_write(service, "POST", "/profiles", {**body, "base_url": url}) for url in BAD_ADDRESSES]
        edited = [_write(service, "PUT", f"/profiles/{profile['id']}", {"base_url": url}) for url in BAD_ADDRESSES]
        listed = _listed(service, profile["id"])

    assert [r.status_code for r in created + edited] == [400] * (2 * len(BAD_ADDRESSES))
    assert all("base_url" in r.json()["error"] for r in created + edited)
    assert listed["base_url"] == model.base_url


@pytest.mark.parametrize("url", ["not a url", "ftp://x", "http://[::1"])
def test_the_connection_test_of_an_invalid_address_in_the_file_answers(start_service, url):
    """A profile file edited outside the service: the test still answers, "地址无效"."""
    with FakeModel() as model:
        service = start_service(model)
        profile = _create(service, model)
        path = service.profiles_dir / f"{profile['id']}.env"
        path.write_text(path.read_text("utf-8").replace(model.base_url, url), encoding="utf-8")
        response = _write(service, "POST", f"/profiles/{profile['id']}/test")
        assert model.requests == []

    assert response.status_code == 200, response.text
    assert response.json()["result"] == "invalid_address"
    assert response.json()["message"].startswith("地址无效")
    assert KEY not in response.text


@pytest.mark.parametrize("fault", ["null_content", "list_content", "object_content"])
def test_an_answer_whose_content_is_not_text_is_unparseable(start_service, fault):
    with FakeModel() as model:
        model.inject_error(fault, kind="connection_test")
        service = start_service(model)
        profile = _create(service, model)
        response = _write(service, "POST", f"/profiles/{profile['id']}/test")

    assert response.status_code == 200, response.text
    assert response.json()["result"] == "unparseable"


# M4 ticket 12: a profile's "stream" setting (streamed requests, ZT_STREAM).
def test_a_profile_streams_its_requests_once_stream_is_set_and_tested_again(start_service):
    with FakeModel() as gemini, FakeModel() as deepseek:
        service = start_service(gemini)
        profile = _create(service, deepseek)
        assert profile["stream"] is False  # the default
        path = f"/profiles/{profile['id']}"
        file = service.profiles_dir / f"{profile['id']}.env"
        assert "ZT_STREAM" not in file.read_text("utf-8")  # off: the file as before ticket 12
        assert _write(service, "POST", f"{path}/test").json()["ok"]
        assert _write(service, "POST", f"{path}/apply").status_code == 200

        streamed = _write(service, "PUT", path, {"stream": True})
        assert streamed.status_code == 200, streamed.text
        assert streamed.json()["stream"] is True
        assert not streamed.json()["tested"]  # a new connection setting
        assert _profiles(service)["current"] == "gemini"
        assert sum(CURRENT_RESET in line for line in service.output) == 1
        assert 'ZT_STREAM="1"' in file.read_text("utf-8")
        assert _listed(service, profile["id"])["stream"] is True

        # The connection test sends as the profile does: streamed.
        tested = _write(service, "POST", f"{path}/test").json()
        assert tested["ok"], tested
        first, second = deepseek.requests
        assert "stream" not in first.body
        assert second.body["stream"] is True
        assert _listed(service, profile["id"])["tested"]

        # A stream that breaks off is not a passed test.
        deepseek.inject_error("stream_cut", kind="connection_test")
        cut = _write(service, "POST", f"{path}/test").json()
        assert cut["result"] == "unparseable", cut
        assert not _listed(service, profile["id"])["tested"]


def test_a_test_passed_before_ticket_12_still_counts_for_a_profile_with_stream_off():
    """The fingerprint of a profile with streaming off is the one ticket 02 saved."""
    import hashlib

    from zotero_translate.profiles import Profile

    profile = Profile(id="0123456789ab", name="D", base_url="http://x/v1", api_key="k", model="m")
    saved_by_ticket_02 = hashlib.sha256(b'["http://x/v1", "k", "m", null]').hexdigest()
    assert profile.fingerprint() == saved_by_ticket_02
    assert Profile(**{**profile.__dict__, "stream": True}).fingerprint() != saved_by_ticket_02


def test_a_profile_created_with_stream_on_says_so(start_service):
    with FakeModel() as model:
        service = start_service(model)
        assert _create(service, model, stream=True)["stream"] is True


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_a_stream_setting_that_is_not_true_or_false_is_refused(start_service, value):
    with FakeModel() as model:
        service = start_service(model)
        body = {"name": "DeepSeek", "base_url": model.base_url, "api_key": KEY, "model": PROFILE_MODEL, "stream": value}
        response = _write(service, "POST", "/profiles", body)
        assert response.status_code == 400, response.text
        assert "stream" in response.json()["error"]


def test_an_error_building_the_client_is_redacted(monkeypatch):
    """The text of an exception raised while the client is built goes through the same redaction."""
    from zotero_translate import model_client
    from zotero_translate.settings import ModelSettings

    def broken_endpoint(settings):
        raise ValueError(f"cannot use Authorization: Bearer {settings.api_key}")

    monkeypatch.setattr(model_client, "Endpoint", broken_endpoint)
    check = model_client.check_connection(
        ModelSettings(base_url="http://127.0.0.1:9/v1", api_key=KEY, model=PROFILE_MODEL)
    )
    assert check.result == "invalid_address"
    assert KEY not in check.message and model_client.REDACTED in check.message
