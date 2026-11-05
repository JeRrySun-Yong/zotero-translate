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
