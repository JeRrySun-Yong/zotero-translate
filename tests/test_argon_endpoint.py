"""Two pipeline model endpoints: argon for the dossier and term list, timeouts forwarded to argon.

Ticket closure-01 and ADR 0004. Two fake pipeline models stand for flash
(translation batches, review, algorithms, paragraph redos) and argon (the
paper dossier, the term list and the promoted glosses request). A flash
request that times out twice in a row sends its remaining attempts to argon;
only timeouts count, and the attempts stay 4 in total. These tests run with
hedging off; hedging replaces the rule for translation batches and reviews
(M3 ticket 15, ``test_hedging.py``).
"""

import json
import subprocess
import sys

import pytest

from tests.conftest import ARGON_MODEL_NAME
from tests.conftest import EXCERPT
from tests.conftest import FAKE_API_KEY
from tests.conftest import FAKE_ARGON_API_KEY
from tests.conftest import FAKE_MODEL_NAME
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.conftest import write_env_file
from tests.fake_model import FakeModel

SMALL_TIMEOUT = 2.0  # seconds; production uses 480
FLASH_KINDS = {"translate_batch", "review", "single_paragraph", "algorithm", "algorithm_review"}


def _messages(request) -> str:
    """The request without its model name: a forwarded request differs only in the model."""
    return json.dumps(request.body["messages"], ensure_ascii=False)


def _sends(model: FakeModel, request) -> list:
    return [r for r in model.requests if _messages(r) == _messages(request)]


@pytest.fixture(scope="module")
def clean_run(tmp_path_factory):
    """The excerpt translated with no fault; flash and argon are separate fakes."""
    with FakeModel() as flash, FakeModel() as argon:
        service = launch_service(flash, tmp_path_factory.mktemp("argon-clean"), argon=argon)
        try:
            task_id = service.submit(EXCERPT)
            final = service.wait(task_id)
            report = service.report(task_id)
            task_dir = service.data_dir / "tasks" / task_id
            files = {
                path.name: path.read_bytes() for path in task_dir.rglob("*") if path.is_file()
            }
            responses = [
                service.http.get(path).content
                for path in ("/health", f"/tasks/{task_id}", f"/tasks/{task_id}/files/mono")
            ]
        finally:
            stop_service(service)
    return {
        "flash": flash,
        "argon": argon,
        "final": final,
        "report": report,
        "files": files,
        "responses": responses,
    }


def test_dossier_and_term_list_go_to_argon_only_and_translation_to_flash_only(clean_run):
    flash, argon = clean_run["flash"], clean_run["argon"]

    assert clean_run["final"]["state"] == "succeeded", clean_run["final"]["error"]
    assert len(argon.requests_of("dossier")) == 1
    assert len(argon.requests_of("term_list")) == 1
    assert {r.kind for r in argon.requests} <= {"dossier", "term_list", "promoted_glosses"}
    assert flash.requests_of("dossier") == flash.requests_of("term_list") == []
    assert flash.requests_of("translate_batch") and flash.requests_of("review")
    assert {r.kind for r in flash.requests} <= FLASH_KINDS
    for request in argon.requests:
        assert request.body["model"] == ARGON_MODEL_NAME
        assert request.body["reasoning_effort"] == "high"
        assert request.headers["Authorization"] == f"Bearer {FAKE_ARGON_API_KEY}"
    for request in flash.requests:
        assert request.body["model"] == FAKE_MODEL_NAME
        assert request.body["reasoning_effort"] == "high"
        assert request.headers["Authorization"] == f"Bearer {FAKE_API_KEY}"


def test_task_report_records_the_model_of_each_stage_and_each_endpoint(clean_run):
    report = clean_run["report"]
    stages = {stage["stage"]: stage for stage in report["stages"]}

    assert stages["生成论文档案"]["models"] == {ARGON_MODEL_NAME: {"requests": 1, "timeouts": 0}}
    assert stages["生成本篇术语清单"]["models"] == {
        ARGON_MODEL_NAME: {"requests": 1, "timeouts": 0}
    }
    translate = stages["翻译与审校"]["models"]
    assert set(translate) == {FAKE_MODEL_NAME}
    assert translate[FAKE_MODEL_NAME]["requests"] > 0
    assert translate[FAKE_MODEL_NAME]["timeouts"] == 0

    endpoints = report["model"]["endpoints"]
    assert endpoints["flash"]["model"] == FAKE_MODEL_NAME
    assert endpoints["flash"]["requests"] == len(clean_run["flash"].requests)
    assert endpoints["flash"]["max_in_flight"] >= 1
    assert endpoints["argon"]["model"] == ARGON_MODEL_NAME
    assert endpoints["argon"]["concurrency"] == 16
    assert endpoints["argon"]["requests"] == len(clean_run["argon"].requests)
    assert endpoints["argon"]["max_in_flight"] == 1
    assert report["model"]["forwarded"] == {"requests": 0, "pages": []}


def test_an_argon_key_echoed_in_a_400_response_is_redacted(start_service):
    with FakeModel() as flash, FakeModel() as argon:
        flash.fail_same_request(["timeout", "timeout"], kind="translate_batch")
        argon.fail_same_request(["400_echo", "400_echo"], kind="translate_batch")
        service = start_service(flash, argon=argon, request_timeout=SMALL_TIMEOUT, hedging=False)

        task_id = service.submit()
        final = service.wait_first_failure(task_id)

    assert final["state"] == "waiting_rerun"  # the attempt failed; retryable (M3 ticket 05)
    assert "Bearer [redacted]" in final["error"], final["error"]
    for key in (FAKE_API_KEY, FAKE_ARGON_API_KEY):
        assert key not in final["error"]
        for path in service.data_dir.rglob("*"):
            if path.is_file():
                assert key.encode() not in path.read_bytes(), path.name


def test_neither_api_key_appears_in_the_task_directory_or_http_responses(clean_run):
    for key in (FAKE_API_KEY, FAKE_ARGON_API_KEY):
        for name, data in clean_run["files"].items():
            assert key.encode() not in data, name
        for data in clean_run["responses"]:
            assert key.encode() not in data


# -- startup -------------------------------------------------------------


def _start(command: str, argon_env_file, tmp_path, fake_model) -> subprocess.CompletedProcess:
    flash_env = write_env_file(tmp_path, fake_model.base_url, FAKE_API_KEY)
    target = ["serve", "--port", "0"] if command == "serve" else [
        "run",
        str(EXCERPT),
        "--output-dir",
        str(tmp_path / "out"),
    ]
    return subprocess.run(
        [sys.executable, "-m", "zotero_translate", *target]
        + ["--env-file", str(flash_env), "--argon-env-file", str(argon_env_file)]
        + ["--data-dir", str(tmp_path / "data")],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("command", ["serve", "run"])
def test_a_missing_argon_env_file_stops_run_and_serve_at_startup(
    command, tmp_path, fake_model
):
    missing = tmp_path / "no-such-argon.env"

    result = _start(command, missing, tmp_path, fake_model)

    assert result.returncode == 2
    assert "argon" in result.stderr and str(missing) in result.stderr
    assert "找不到" in result.stderr
    assert fake_model.requests == []


@pytest.mark.parametrize("command", ["serve", "run"])
def test_an_argon_env_file_without_key_stops_run_and_serve_at_startup(
    command, tmp_path, fake_model
):
    incomplete = tmp_path / "argon.env"
    incomplete.write_text(f"OPENAI_BASE_URL={fake_model.base_url}\n", encoding="utf-8")

    result = _start(command, incomplete, tmp_path, fake_model)

    assert result.returncode == 2
    assert "缺少：OPENAI_API_KEY" in result.stderr
    assert fake_model.requests == []


# -- forwarding after two timeouts in a row --------------------------------
# With hedging off (``--no-hedging``): with it on, a translation batch or a
# review goes to argon after one timeout (``test_hedging.py``).


@pytest.fixture(scope="module")
def forwarded_batch(tmp_path_factory):
    """A translation batch times out twice on flash and succeeds on argon; then a resubmit."""
    with FakeModel() as flash, FakeModel() as argon:
        flash.fail_same_request(["timeout", "timeout"], kind="translate_batch")
        service = launch_service(
            flash,
            tmp_path_factory.mktemp("argon-forward"),
            argon=argon,
            request_timeout=SMALL_TIMEOUT,
            hedging=False,
        )
        try:
            first = service.wait(service.submit(EXCERPT))
            first_flash, first_argon = list(flash.requests), list(argon.requests)
            second = service.wait(service.submit(EXCERPT))
            reports = [service.report(first["task_id"]), service.report(second["task_id"])]
        finally:
            stop_service(service)
    return {
        "flash": flash,
        "argon": argon,
        "first": first,
        "second": second,
        "first_flash": first_flash,
        "first_argon": first_argon,
        "reports": reports,
    }


def test_a_batch_timing_out_twice_on_flash_succeeds_on_argon(forwarded_batch):
    first = forwarded_batch["first"]
    assert first["state"] == "succeeded", first["error"]
    [timed_out, again] = [r for r in forwarded_batch["first_flash"] if r.fault]
    assert (timed_out.fault, again.fault) == ("timeout", "timeout")
    assert _messages(timed_out) == _messages(again)
    [forwarded] = _sends_in(forwarded_batch["first_argon"], timed_out)
    assert forwarded.kind == "translate_batch"
    assert forwarded.body["model"] == ARGON_MODEL_NAME
    assert len(_sends_in(forwarded_batch["first_flash"], timed_out)) == 2

    report = forwarded_batch["reports"][0]
    assert report["model"]["forwarded"] == {"requests": 1, "pages": [1]}
    assert report["model"]["endpoints"]["flash"]["timeouts"] == 2
    translate = {s["stage"]: s for s in report["stages"]}["翻译与审校"]["models"]
    assert translate[FAKE_MODEL_NAME]["timeouts"] == 2
    assert translate[ARGON_MODEL_NAME] == {"requests": 1, "timeouts": 0}


def test_a_resubmit_finds_the_forwarded_answer_in_the_cache(forwarded_batch):
    second = forwarded_batch["second"]
    assert second["state"] == "succeeded", second["error"]
    [timed_out] = [r for r in forwarded_batch["first_flash"] if r.fault][:1]
    flash_later = forwarded_batch["flash"].requests[len(forwarded_batch["first_flash"]) :]
    argon_later = forwarded_batch["argon"].requests[len(forwarded_batch["first_argon"]) :]

    assert _sends_in(flash_later, timed_out) == []
    assert _sends_in(argon_later, timed_out) == []
    assert forwarded_batch["reports"][1]["model"]["forwarded"] == {"requests": 0, "pages": []}


def _sends_in(requests: list, request) -> list:
    return [r for r in requests if _messages(r) == _messages(request)]


def test_a_review_timing_out_twice_is_forwarded_and_the_task_succeeds(start_service):
    with FakeModel() as flash, FakeModel() as argon:
        flash.fail_same_request(["timeout", "timeout"], kind="review")
        service = start_service(flash, argon=argon, request_timeout=SMALL_TIMEOUT, hedging=False)

        final = service.wait(service.submit())

        assert final["state"] == "succeeded", final["error"]
        [timed_out, _] = [r for r in flash.requests if r.fault]
        [forwarded] = _sends(argon, timed_out)
        assert forwarded.kind == "review"
        assert service.report(final["task_id"])["model"]["forwarded"] == {
            "requests": 1,
            "pages": [1],
        }


def test_one_timeout_then_success_is_not_forwarded(start_service):
    with FakeModel() as flash, FakeModel() as argon:
        flash.fail_same_request(["timeout"], kind="translate_batch")
        service = start_service(flash, argon=argon, request_timeout=SMALL_TIMEOUT, hedging=False)

        final = service.wait(service.submit())

        assert final["state"] == "succeeded", final["error"]
        [timed_out] = [r for r in flash.requests if r.fault]
        assert [r.fault for r in _sends(flash, timed_out)] == ["timeout", None]
        assert _sends(argon, timed_out) == []
        assert service.report(final["task_id"])["model"]["forwarded"]["requests"] == 0


def test_a_500_between_two_timeouts_sets_the_count_back_and_the_fourth_attempt_stays_on_flash(
    start_service,
):
    with FakeModel() as flash, FakeModel() as argon:
        flash.fail_same_request(["timeout", "500", "timeout"], kind="translate_batch")
        service = start_service(flash, argon=argon, request_timeout=SMALL_TIMEOUT, hedging=False)

        final = service.wait(service.submit())

        assert final["state"] == "succeeded", final["error"]
        [timed_out] = [r for r in flash.requests if r.fault][:1]
        assert [r.fault for r in _sends(flash, timed_out)] == ["timeout", "500", "timeout", None]
        assert _sends(argon, timed_out) == []
        assert service.report(final["task_id"])["model"]["forwarded"]["requests"] == 0


def test_a_forwarded_request_failing_twice_on_argon_fails_the_task_with_its_page(start_service):
    with FakeModel() as flash, FakeModel() as argon:
        flash.fail_same_request(["timeout", "timeout"], kind="translate_batch")
        argon.fail_same_request(["500", "timeout"], kind="translate_batch")
        service = start_service(flash, argon=argon, request_timeout=SMALL_TIMEOUT, hedging=False)

        final = service.wait_first_failure(service.submit())

        assert final["state"] == "waiting_rerun"  # the attempt failed; retryable (M3 ticket 05)
        assert "第 1 页" in final["error"], final["error"]
        [timed_out] = [r for r in flash.requests if r.fault][:1]
        assert [r.fault for r in _sends(flash, timed_out)] == ["timeout", "timeout"]
        assert [r.fault for r in _sends(argon, timed_out)] == ["500", "timeout"]
        report = service.report(final["task_id"])
        assert report["failed_batches"][0]["page"] == 1
        assert report["model"]["forwarded"] == {"requests": 1, "pages": [1]}


def test_requests_forwarded_at_once_stay_within_argons_concurrency_limit(tmp_path):
    with FakeModel() as flash, FakeModel(delay=2.0) as argon:
        for _ in range(4):  # the first 4 different batches
            flash.fail_same_request(["timeout", "timeout"], kind="translate_batch")
        # Argon answers after 2 s, so it needs a longer timeout than flash.
        argon_env = write_env_file(
            tmp_path, argon.base_url, FAKE_ARGON_API_KEY, request_timeout=10, concurrency=2
        )
        service = launch_service(
            flash, tmp_path, request_timeout=SMALL_TIMEOUT, argon_env_file=argon_env, hedging=False
        )
        try:
            final = service.wait(service.submit())
            report = service.report(final["task_id"])
        finally:
            stop_service(service)

    assert final["state"] == "succeeded", final["error"]
    assert len(argon.requests_of("translate_batch")) == 4
    assert argon.max_in_flight == 2
    assert report["model"]["forwarded"] == {"requests": 4, "pages": [1, 1, 1, 1]}
    assert report["model"]["endpoints"]["argon"]["concurrency"] == 2
    assert report["model"]["endpoints"]["argon"]["max_in_flight"] == 2
