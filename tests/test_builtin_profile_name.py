"""The built-in model profile's name comes from ``model.env`` (M5 ticket 02).

``ZT_PROFILE_NAME`` in the flash environment file (``--env-file``) names the
built-in profile; without it, or with an empty value, it is still "Gemini".
Only the name changes: its id stays ``gemini``, and the name never reaches a
model request, so the request bodies and cache keys are as before. A name
the user profiles would refuse (over 60 characters, a control character)
stops ``serve`` and ``run`` at startup.

The tests that run a paper task use the fake model; they are the only ones in
this file that need ``heavy.sh``.
"""

import json
import subprocess
import sys

import pytest

from tests import sample_prompts
from tests.conftest import EXCERPT
from tests.conftest import FAKE_API_KEY
from tests.conftest import FAKE_ARGON_API_KEY
from tests.conftest import FAKE_MODEL_NAME
from tests.conftest import launch_service
from tests.conftest import serve_command
from tests.conftest import stop_service
from tests.conftest import write_env_file
from tests.fake_model import FakeModel
from tests.test_model_profiles import OTHER_KEY
from tests.test_model_profiles import _create
from tests.test_model_profiles import _profiles
from tests.test_model_profiles import _write
from zotero_translate import profiles
from zotero_translate import settings as zt_settings
from zotero_translate.model_client import ModelClient
from zotero_translate.model_client import ResponseCache
from zotero_translate.request_log import REQUESTS_FILE
from zotero_translate.request_log import load_records
from zotero_translate.service import _task_profile
from zotero_translate.settings import SettingsError
from zotero_translate.settings import load_pipeline_settings

NAME = "GPT-5 Mini"
NAME_LINE = f"ZT_PROFILE_NAME={NAME}\n"
LONGEST = "n" * 60


def _env_file(root, model, line: str = NAME_LINE):
    """The flash environment file ``launch_service`` would write, plus ``line``."""
    path = write_env_file(root, model.base_url, FAKE_API_KEY, FAKE_MODEL_NAME)
    with path.open("a", encoding="utf-8") as file:
        file.write(line)
    return path


def _argon_env_file(root, model):
    return write_env_file(root, model.base_url, FAKE_ARGON_API_KEY)


def _builtin(service) -> dict:
    [builtin] = [p for p in _profiles(service)["profiles"] if p["builtin"]]
    return builtin


# -- the name in the service ---------------------------------------------------


def test_the_env_file_names_the_builtin_profile_and_its_id_stays_gemini(tmp_path):
    with FakeModel() as model:
        service = launch_service(model, tmp_path, env_file=_env_file(tmp_path, model))
        try:
            listing = _profiles(service)
            one = service.http.get("/profiles/gemini")
            applied = _write(service, "POST", "/profiles/gemini/apply")
        finally:
            stop_service(service)

    assert listing["current"] == "gemini"
    [builtin] = listing["profiles"]
    assert builtin["id"] == "gemini" and builtin["name"] == NAME
    assert builtin["builtin"] and builtin["current"] and builtin["tested"]
    assert one.status_code == 200 and one.json() == builtin
    assert applied.status_code == 200, applied.text
    assert applied.json() == {"current": "gemini", "name": NAME}
    assert "Gemini" not in json.dumps(listing)


def test_a_submitted_task_records_the_name(tmp_path):
    pause = tmp_path / "pause"
    pause.mkdir()
    with FakeModel() as model:
        service = launch_service(
            model,
            tmp_path,
            env_file=_env_file(tmp_path, model),
            env={"ZT_TEST_PAUSE_AFTER_MARKER": str(pause)},  # no task gets past its process marker
        )
        try:
            task_id = service.submit()
            named = service.http.post(
                "/tasks",
                files={"file": ("paper.pdf", EXCERPT.read_bytes(), "application/pdf")},
                data={"filename": "paper.pdf", "profile": "gemini"},
            )
            status = service.status(task_id)
            recorded = json.loads((service.data_dir / "tasks" / task_id / "task.json").read_text("utf-8"))
        finally:
            stop_service(service)

    assert status["profile"] == {"id": "gemini", "name": NAME}
    assert named.status_code == 201, named.text
    assert named.json()["profile"] == {"id": "gemini", "name": NAME}
    assert (recorded["profile_id"], recorded["profile_name"]) == ("gemini", NAME)


def test_the_errors_about_the_builtin_profile_name_it(tmp_path):
    with FakeModel() as model:
        service = launch_service(model, tmp_path, env_file=_env_file(tmp_path, model))
        try:
            refused = [
                _write(service, "DELETE", "/profiles/gemini"),
                _write(service, "PUT", "/profiles/gemini", {"name": "x"}),
                _write(service, "POST", "/profiles/gemini/test"),
            ]
        finally:
            stop_service(service)

    assert [r.status_code for r in refused] == [409, 409, 409]
    for response in refused:
        assert NAME in response.json()["error"], response.text
        assert "Gemini" not in response.json()["error"]


@pytest.mark.parametrize(
    "line",
    ["", "ZT_PROFILE_NAME=\n", 'ZT_PROFILE_NAME=""\n', 'ZT_PROFILE_NAME="   "\n'],
    ids=["no-key", "empty", "empty-quoted", "blank-quoted"],
)
def test_without_a_name_the_builtin_profile_is_still_gemini(tmp_path, line):
    with FakeModel() as model:
        service = launch_service(model, tmp_path, env_file=_env_file(tmp_path, model, line))
        try:
            builtin = _builtin(service)
            refused = _write(service, "DELETE", "/profiles/gemini")
        finally:
            stop_service(service)

    assert (builtin["id"], builtin["name"]) == ("gemini", "Gemini")
    assert refused.status_code == 409
    assert refused.json()["error"] == "内置配置 Gemini 不能删除"


def test_the_quoted_name_and_its_spaces_are_taken_as_written(tmp_path):
    with FakeModel() as model:
        env_file = _env_file(tmp_path, model, f'ZT_PROFILE_NAME="  {NAME}  "\n')
        service = launch_service(model, tmp_path, env_file=env_file)
        try:
            assert _builtin(service)["name"] == NAME  # stripped, as a user profile's name is
        finally:
            stop_service(service)


def test_the_name_shows_where_the_current_profile_goes_back_to_the_builtin_one(tmp_path):
    with FakeModel() as model:
        service = launch_service(model, tmp_path, env_file=_env_file(tmp_path, model))
        try:
            profile = _create(service, model)
            assert _write(service, "POST", f"/profiles/{profile['id']}/test").json()["ok"]
            assert _write(service, "POST", f"/profiles/{profile['id']}/apply").status_code == 200
            # A new key is untested: the current profile goes back to the built-in one.
            assert _write(service, "PUT", f"/profiles/{profile['id']}", {"api_key": OTHER_KEY}).status_code == 200
            assert _profiles(service)["current"] == "gemini"
        finally:
            stop_service(service)

    [reset] = [line for line in service.output if "event=当前配置改回" in line]
    assert f"event=当前配置改回 {NAME} " in reset and f"profile={profile['id']}" in reset


def test_a_rename_between_runs_keeps_the_current_user_profile(tmp_path):
    with FakeModel() as model:
        first = launch_service(model, tmp_path)
        try:
            profile = _create(first, model)
            assert _write(first, "POST", f"/profiles/{profile['id']}/test").json()["ok"]
            assert _write(first, "POST", f"/profiles/{profile['id']}/apply").status_code == 200
            assert _builtin(first)["name"] == "Gemini"
        finally:
            stop_service(first)
        second = launch_service(
            model, tmp_path, data_dir=first.data_dir, env_file=_env_file(tmp_path, model)
        )
        try:
            listing = _profiles(second)
        finally:
            stop_service(second)

    assert listing["current"] == profile["id"]  # model-profiles.json is by id
    names = {p["id"]: p["name"] for p in listing["profiles"]}
    assert names == {"gemini": NAME, profile["id"]: "DeepSeek"}
    assert not any("event=当前配置改回" in line for line in second.output)


def test_a_task_from_before_profiles_has_the_builtin_profile_of_now(tmp_path):
    directory = tmp_path / "task"
    directory.mkdir()
    (directory / "task.json").write_text(json.dumps({"submitted_at": 1.0}), encoding="utf-8")
    assert _task_profile(directory, NAME) == {"id": "gemini", "name": NAME}
    (directory / "task.json").write_text(
        json.dumps({"profile_id": "gemini", "profile_name": "Gemini"}), encoding="utf-8"
    )
    assert _task_profile(directory, NAME) == {"id": "gemini", "name": "Gemini"}  # as recorded


# -- the name's rules ------------------------------------------------------------


@pytest.mark.parametrize(
    "name, reason",
    [("n" * 61, "60"), ("two\tparts", "控制字符"), ("zero\u200bwidth", "控制字符")],
    ids=["too-long", "tab", "zero-width-space"],
)
def test_serve_and_run_refuse_a_bad_name_at_startup(tmp_path, name, reason):
    with FakeModel() as model:
        env_file = _env_file(tmp_path, model, f'ZT_PROFILE_NAME="{name}"\n')
        argon_env_file = _argon_env_file(tmp_path, model)
        serve = subprocess.run(
            serve_command(env_file, argon_env_file, tmp_path / "data")
            + ["--profiles-dir", str(tmp_path / "profiles")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        run = subprocess.run(
            [
                sys.executable, "-m", "zotero_translate", "run", str(EXCERPT),
                "--output-dir", str(tmp_path / "out"),
                "--env-file", str(env_file),
                "--argon-env-file", str(argon_env_file),
                "--data-dir", str(tmp_path / "run-data"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    for command in (serve, run):
        assert command.returncode == 2, (command.args, command.stdout, command.stderr)
        assert "ZT_PROFILE_NAME" in command.stderr and reason in command.stderr
        assert str(env_file) in command.stderr
    assert not (tmp_path / "data" / "service.lock").exists()  # the service never started
    assert not (tmp_path / "out" / "source.pdf").exists()  # no task started
    assert model.requests == []


def test_the_name_has_the_limits_of_a_user_profiles_name(tmp_path):
    def builtin_accepts(name: str) -> bool:
        path = tmp_path / "endpoint.env"
        path.write_text(
            f'OPENAI_BASE_URL=http://127.0.0.1:9/v1\nOPENAI_API_KEY=sk-x\nZT_PROFILE_NAME="{name}"\n',
            encoding="utf-8",
        )
        try:
            return zt_settings.load_builtin_profile_name(path) == name.strip() != ""
        except SettingsError:
            return False

    def user_profile_accepts(name: str) -> bool:
        try:
            return profiles._validate({"name": name}, required=()) == {"name": name.strip()}
        except profiles.ProfileError:
            return False

    samples = [LONGEST, LONGEST + "n", "名" * 60, "名" * 61, "a\tb", "a\u200bb", "a b", "GPT-5 Mini", "a  b"]
    for name in samples:
        assert builtin_accepts(name) == user_profile_accepts(name), repr(name)
    assert builtin_accepts(LONGEST) and not builtin_accepts(LONGEST + "n")


def test_a_missing_or_unreadable_file_has_no_name_to_read(tmp_path):
    with pytest.raises(SettingsError, match="找不到"):
        zt_settings.load_builtin_profile_name(tmp_path / "missing.env")


# -- the name stays out of the model requests -----------------------------------

MESSAGES_OF = {
    "batch": sample_prompts.batch_prompt(batch_extra_rules=None),
    "review": sample_prompts.review_prompt(),
}


def _flash_bodies(env_file, argon_env_file) -> dict[str, dict]:
    """The request bodies of the sample paragraph's batch and review prompts, as the flash client builds them."""
    settings = load_pipeline_settings(env_file, argon_env_file)
    assert settings.user_profile_rules is False
    client = ModelClient(settings.flash)
    try:
        return {kind: client._body([{"role": "user", "content": text}]) for kind, text in MESSAGES_OF.items()}
    finally:
        client.close()


def test_a_rename_changes_no_request_body_and_no_cache_key(tmp_path):
    def endpoint(name: str, line: str):
        path = tmp_path / name
        path.write_text(f"OPENAI_BASE_URL=http://127.0.0.1:9/v1\nOPENAI_API_KEY=sk-x\n{line}", encoding="utf-8")
        return path

    plain, renamed = endpoint("plain.env", ""), endpoint("renamed.env", NAME_LINE)
    argon = endpoint("argon.env", "")
    before, after = _flash_bodies(plain, argon), _flash_bodies(renamed, argon)

    assert load_pipeline_settings(plain, argon) == load_pipeline_settings(renamed, argon)
    for kind in MESSAGES_OF:
        # The bytes on the wire (httpx's JSON of the body), and the key of the cache.
        wire = lambda body: json.dumps(body, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
        assert wire(before[kind]) == wire(after[kind])
        assert NAME not in wire(after[kind])
        assert ResponseCache.key(before[kind]) == ResponseCache.key(after[kind])
    # And they are the keys pinned for the built-in profile before M5.
    from tests.test_prompt_protections import GEMINI_BATCH_KEY
    from tests.test_prompt_protections import GEMINI_REVIEW_KEY

    assert ResponseCache.key(after["batch"]) == GEMINI_BATCH_KEY
    assert ResponseCache.key(after["review"]) == GEMINI_REVIEW_KEY


# -- a paper task with the fake model (heavy.sh) ---------------------------------


def test_a_task_after_a_rename_reports_the_name_and_hits_the_cache_of_before(tmp_path):
    """The same paper before and after the rename: the second run finds every
    answer in the first run's cache, so its requests are byte for byte the
    first's; its report and status name the profile as renamed."""
    with FakeModel() as model:
        first = launch_service(model, tmp_path)
        try:
            task_1 = first.submit()
            final_1 = first.wait(task_1)
            report_1 = first.report(task_1)
        finally:
            stop_service(first)
        sent = len(model.requests)

        second = launch_service(
            model, tmp_path, data_dir=first.data_dir, env_file=_env_file(tmp_path, model)
        )
        try:
            task_2 = second.submit()
            final_2 = second.wait(task_2)
            report_2 = second.report(task_2)
            records = load_records(second.data_dir / "tasks" / task_2 / "attempts" / "1" / REQUESTS_FILE)
        finally:
            stop_service(second)

        assert final_1["state"] == final_2["state"] == "succeeded", (final_1["error"], final_2["error"])
        assert sent > 2
        assert len(model.requests) == sent, "the renamed run sent a request the first run had not"

    assert final_1["profile"] == {"id": "gemini", "name": "Gemini"}
    assert final_2["profile"] == {"id": "gemini", "name": NAME}
    assert report_1["profile"]["name"] == "Gemini" and report_1["profile"]["id"] == "gemini"
    assert report_2["profile"]["name"] == NAME and report_2["profile"]["id"] == "gemini"
    assert report_2["profile"]["models"] == report_1["profile"]["models"]
    assert records and {r["result"] for r in records} == {"cache_hit"}
    assert report_2["model"]["user_profile_rules"] is False
