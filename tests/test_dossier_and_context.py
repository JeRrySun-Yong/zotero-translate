"""Paper dossier and the context of each translation batch.

Spec "计划覆盖的行为" 4 (the paper dossier part) and 5: the dossier request
is sent exactly once and contains the full text; every translation batch
carries the condensed dossier, the title of its section and the source text
of its neighbouring paragraphs, within the context budget.
"""

import json
import re

import pymupdf
import pytest

from tests.conftest import FIXTURES
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FAKE_DOSSIER
from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import FakeModel
from tests.fake_model import batch_items

# AutoDesign pages 1 and 16: page 16 starts in "7 Related Work" (whose heading
# is on page 15) and contains the heading "8 Conclusion".
TWO_PAGES = FIXTURES / "autodesign-p1-p16.pdf"
CONTEXT_BUDGET = 6000
NEIGHBOR_WINDOW = 1200
CONTEXT_HEADER = "## Paper context"
DOSSIER_PART = "Paper dossier (condensed)"
SECTION_PART = "Section of this batch"
BEFORE_PART = "Source text before this batch"
AFTER_PART = "Source text after this batch"
# A dossier far larger than the budget, so the condensed version must be cut.
LONG_SUMMARY = "长概要开头。" + "本文提出的框架逐轮改进设计流程。" * 600
LARGE_DOSSIER = {
    **FAKE_DOSSIER,
    "paper_summary_zh": LONG_SUMMARY,
    "translation_pitfalls_zh": [f"易错点 {i}：保持术语一致。" for i in range(200)],
}
# The first words of the paragraph that follows the "8 Conclusion" heading.
CONCLUSION_START = "turns recurring design failures"


def squeeze(text: str) -> str:
    return re.sub(r"\s+", "", text)


def pdf_text(pdf) -> str:
    with pymupdf.open(pdf) as doc:
        return squeeze("".join(page.get_text() for page in doc))


def context_parts(prompt: str) -> tuple[str, dict[str, str]]:
    """The paper context block of a batch prompt and its parts by heading."""
    start = prompt.index(CONTEXT_HEADER)
    end = prompt.find("\n## ", start + len(CONTEXT_HEADER))
    block = prompt[start : end if end != -1 else len(prompt)].rstrip()
    parts = {}
    for chunk in block.split("\n### ")[1:]:
        heading, _, body = chunk.partition("\n")
        parts[heading.strip()] = body.strip()
    return block, parts


def batch_inputs(request) -> str:
    return "\n".join(item["input"] for item in batch_items(request.prompt) or [])


@pytest.fixture(scope="module")
def finished_task(tmp_path_factory):
    with FakeModel() as model:
        model.respond("dossier", lambda body: json.dumps(LARGE_DOSSIER, ensure_ascii=False))
        service = launch_service(model, tmp_path_factory.mktemp("dossier"))
        try:
            task_id = service.submit(TWO_PAGES)
            final = service.wait(task_id)
            task_dir = service.data_dir / "tasks" / task_id
            dossier_file = task_dir / "dossier.json"
            saved = json.loads(dossier_file.read_text("utf-8")) if dossier_file.exists() else None
        finally:
            stop_service(service)
    assert final["state"] == "succeeded", final["error"]
    return {"model": model, "saved_dossier": saved}


def test_dossier_is_requested_once_with_the_full_text_of_every_page(finished_task):
    requests = finished_task["model"].requests_of("dossier")

    assert len(requests) == 1
    sent = squeeze(requests[0].prompt)
    with pymupdf.open(TWO_PAGES) as doc:
        assert len(doc) == 2
        for page in doc:
            assert squeeze(page.get_text()) in sent, f"page {page.number + 1} missing"


def test_dossier_is_sent_before_any_translation_batch(finished_task):
    kinds = [request.kind for request in finished_task["model"].requests]

    assert kinds[0] == "dossier"


def test_dossier_is_saved_in_the_task_directory(finished_task):
    assert finished_task["saved_dossier"] == LARGE_DOSSIER


def test_every_batch_carries_the_three_contexts_within_the_budget(finished_task):
    batches = finished_task["model"].requests_of("translate_batch")
    assert batches

    for request in batches:
        block, parts = context_parts(request.prompt)
        assert len(block) <= CONTEXT_BUDGET, len(block)
        assert parts[DOSSIER_PART].startswith(f"Title: {FAKE_DOSSIER['paper_title']}")
        assert "长概要开头。" in parts[DOSSIER_PART]
        assert parts[SECTION_PART]
        assert parts[BEFORE_PART] or parts[AFTER_PART]
        assert len(parts[BEFORE_PART]) <= NEIGHBOR_WINDOW
        assert len(parts[AFTER_PART]) <= NEIGHBOR_WINDOW


def test_neighbors_are_source_text_even_after_they_were_translated(finished_task):
    for request in finished_task["model"].requests_of("translate_batch"):
        _, parts = context_parts(request.prompt)
        assert PSEUDO_PREFIX not in parts[BEFORE_PART] + parts[AFTER_PART]


def test_batch_after_the_conclusion_heading_names_that_section(finished_task):
    batches = [
        request
        for request in finished_task["model"].requests_of("translate_batch")
        if CONCLUSION_START in batch_inputs(request)
    ]
    assert batches, "no batch holds the conclusion paragraph"

    for request in batches:
        _, parts = context_parts(request.prompt)
        assert "Conclusion" in parts[SECTION_PART]
        # The text before the batch is the source that precedes it in the PDF.
        tail = squeeze(parts[BEFORE_PART])[-60:]
        full = pdf_text(TWO_PAGES)
        assert tail and tail in full
        assert full.index(tail) < full.index(squeeze(CONCLUSION_START))


def test_batch_on_the_first_page_does_not_name_the_conclusion(finished_task):
    batches = [
        request
        for request in finished_task["model"].requests_of("translate_batch")
        if "Transforming multimodal sources" in batch_inputs(request)
    ]
    assert batches

    for request in batches:
        _, parts = context_parts(request.prompt)
        assert "Conclusion" not in parts[SECTION_PART]
        assert "Transforming multimodal sources" not in parts[BEFORE_PART]
