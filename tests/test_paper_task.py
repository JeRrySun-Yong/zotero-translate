"""A paper task submitted to the service, answered by the fake pipeline model.

Spec "计划覆盖的行为" 1 and 3: stages and progress, both PDFs, request
parameters.
"""

import re

import pymupdf
import pytest

from tests.conftest import EXCERPT
from tests.conftest import FAKE_MODEL_NAME
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import FakeModel

STAGES = ["排队中", "解析版面", "翻译与审校", "排版", "完成"]
# The first line of the excerpt's abstract.
SOURCE_PHRASE = "Transforming multimodal sources into condensed and structured media outputs"


def pdf_text(data: bytes) -> str:
    """All text of a PDF with whitespace removed (typesetting reflows lines)."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return re.sub(r"\s+", "", "".join(page.get_text() for page in doc))


@pytest.fixture(scope="module")
def finished_task(tmp_path_factory):
    """One excerpt translated end to end; every status poll is kept."""
    with FakeModel(delay=0.3) as model:
        service = launch_service(model, tmp_path_factory.mktemp("paper-task"))
        try:
            task_id = service.submit(EXCERPT)
            polls = []
            final = service.wait(task_id, on_poll=polls.append)
            mono = service.download(task_id, "mono")
            dual = service.download(task_id, "dual")
        finally:
            stop_service(service)
    return {"model": model, "polls": polls, "final": final, "mono": mono, "dual": dual}


def test_task_passes_through_each_stage_in_order_and_succeeds(finished_task):
    final = finished_task["final"]
    assert final["state"] == "succeeded", final["error"]
    assert final["stage"] == "完成"
    assert final["progress"] == 100
    assert final["error"] is None
    assert set(final["files"]) >= {"mono", "dual"}

    seen = []
    for status in finished_task["polls"]:
        if not seen or seen[-1] != status["stage"]:
            seen.append(status["stage"])
    if seen[0] == "排队中":
        seen = seen[1:]
    assert seen == STAGES[1:]


def test_progress_never_decreases(finished_task):
    progress = [status["progress"] for status in finished_task["polls"]]
    assert progress == sorted(progress)
    assert 0 < len(set(progress))


def test_mono_pdf_holds_the_pseudo_translation(finished_task):
    mono = pdf_text(finished_task["mono"])

    assert PSEUDO_PREFIX in mono


def test_dual_pdf_holds_both_source_and_pseudo_translation(finished_task):
    mono = pdf_text(finished_task["mono"])
    dual = pdf_text(finished_task["dual"])
    phrase = re.sub(r"\s+", "", SOURCE_PHRASE)

    assert PSEUDO_PREFIX in dual
    # The pseudo-translation keeps the English, so mono has the phrase once;
    # dual also has the source page.
    assert dual.count(phrase) > mono.count(phrase) >= 1


def test_requests_use_configured_model_high_reasoning_and_temperature_1(finished_task):
    requests = finished_task["model"].requests
    assert requests, "the paper task sent no model request"

    for request in requests:
        assert request.path == "/v1/chat/completions"
        assert request.body["model"] == FAKE_MODEL_NAME
        assert request.body["reasoning_effort"] == "high"
        assert request.body["temperature"] == 1.0
