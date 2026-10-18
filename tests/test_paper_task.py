"""A paper task submitted to the service, answered by the fake pipeline model.

Spec "计划覆盖的行为" 1, 3 and 13: stages and progress, both PDFs, request
parameters, requests in flight up to the concurrency limit.
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

CONCURRENCY = 4
STAGES = [
    "排队中",
    "读取全文",
    "生成论文档案",
    "生成本篇术语清单",
    "解析版面",
    "翻译与审校",
    "排版",
    "完成",
]
# Part of the watermark sentence the engine adds unless told not to.
WATERMARK_MARK = "funstory.ai"
# The first line of the excerpt's abstract.
SOURCE_PHRASE = "Transforming multimodal sources into condensed and structured media outputs"


def pdf_text(data: bytes) -> str:
    """All text of a PDF with whitespace removed (typesetting reflows lines)."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return re.sub(r"\s+", "", "".join(page.get_text() for page in doc))


@pytest.fixture(scope="module")
def finished_task(tmp_path_factory):
    """One excerpt translated end to end; every status poll is kept.

    The engine also starts at most ``CONCURRENCY`` requests per second, so
    the fake model's delay must exceed about 0.75 s for 4 to overlap.
    """
    with FakeModel(delay=1.0) as model:
        service = launch_service(model, tmp_path_factory.mktemp("paper-task"), CONCURRENCY)
        try:
            task_id = service.submit(EXCERPT)
            polls = []
            final = service.wait(task_id, on_poll=polls.append)
            mono = service.download(task_id, "mono")
            dual = service.download(task_id, "dual")
            report = service.report(task_id)
        finally:
            stop_service(service)
    return {
        "model": model,
        "polls": polls,
        "final": final,
        "mono": mono,
        "dual": dual,
        "report": report,
    }


def test_task_passes_through_each_stage_in_order_and_succeeds(finished_task):
    final = finished_task["final"]
    assert final["state"] == "succeeded", final["error"]
    assert final["stage"] == "完成"
    assert final["progress"] == 100
    assert final["error"] is None
    assert final["files"] == ["mono", "dual", "terms"]

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
    assert any(0 < p < 100 for p in progress), "no progress seen while running"


def test_mono_pdf_holds_the_pseudo_translation(finished_task):
    mono = pdf_text(finished_task["mono"])

    assert PSEUDO_PREFIX in mono


def test_pdfs_carry_no_engine_watermark(finished_task):
    for name in ("mono", "dual"):
        assert WATERMARK_MARK not in pdf_text(finished_task[name]), name


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


def test_no_automatic_term_extraction_requests(finished_task):
    """Only the paper dossier, term list, paragraph translation and review requests reach the model.

    One-paragraph requests may occur: the engine redoes a paragraph whose
    answer is more than 3x its input in tokens (see backlog, ticket 02).
    """
    kinds = {request.kind for request in finished_task["model"].requests}

    assert "translate_batch" in kinds
    assert kinds <= {"dossier", "term_list", "translate_batch", "review", "single_paragraph"}


def test_requests_in_flight_reach_the_concurrency_limit(finished_task):
    assert finished_task["model"].max_in_flight == CONCURRENCY
    assert finished_task["report"]["model"]["max_in_flight"] == CONCURRENCY


def test_task_report_records_stages_and_model_calls(finished_task):
    report = finished_task["report"]
    model = finished_task["model"]

    assert report["state"] == "succeeded"
    assert [stage["stage"] for stage in report["stages"]] == STAGES[1:-1]
    assert all(stage["seconds"] >= 0 for stage in report["stages"])
    assert report["model"]["requests"] == len(model.requests)
    assert report["model"]["retries"] == 0
    assert report["failed_batches"] == []
