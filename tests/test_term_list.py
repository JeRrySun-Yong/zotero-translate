"""The paper's term list: one request, checked against the source, injected as a glossary.

Spec "计划覆盖的行为" 4 (the term list part), 6 and 9: the term list request
is sent exactly once and contains the paper dossier and the full text; key
concepts are injected with a target equal to the source, basic terms with
their Chinese rendering; entries that do not appear in the source are
dropped and written into the task report; ``/terms`` can be downloaded.
"""

import json
import re

import pymupdf
import pytest

from tests.conftest import FIXTURES
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FAKE_DOSSIER
from tests.fake_model import FakeModel
from tests.fake_model import batch_items

# AutoDesign pages 1 and 16. Page 16 breaks "Agent-as-a-Judge" after "Agent-"
# at a line end; page 1 breaks "autonomous long-horizon loop" across lines.
TWO_PAGES = FIXTURES / "autodesign-p1-p16.pdf"
TERM_STAGE = "生成本篇术语清单"
INVENTED_KEY = "self-referential poster compiler"
INVENTED_BASIC = "gradient checkpointing"
INVENTED_SPELLING = "recursively self-improving harness"


def term(english, category, chinese=None, gloss_zh=None):
    return {
        "english": english,
        "category": category,
        "chinese": chinese,
        "gloss_zh": gloss_zh,
        "reason": "测试依据。",
    }


MODEL_TERMS = {
    "terms": [
        term(["meta-harness", "Meta-Harness"], "key_concept", gloss_zh="优化框架的外层框架"),
        term(["Agent-as-a-Judge"], "key_concept", gloss_zh="让智能体充当评委的评测方式"),
        term(
            ["recursive self-improvement", INVENTED_SPELLING],
            "key_concept",
            gloss_zh="系统反复改进自身的过程",
        ),
        term(["code agent"], "basic_term", chinese="代码智能体"),
        # Upper case and a double space; the source has "autonomous\nlong-horizon loop".
        term(["AUTONOMOUS  long-horizon loop"], "basic_term", chinese="自主长程循环"),
        term([INVENTED_KEY], "key_concept", gloss_zh="编造的概念"),
        term([INVENTED_BASIC], "basic_term", chinese="梯度检查点"),
    ]
}
KEPT = {
    "meta-harness": ("key_concept", "keep_english"),
    "Agent-as-a-Judge": ("key_concept", "keep_english"),
    "recursive self-improvement": ("key_concept", "keep_english"),
    "code agent": ("basic_term", "chinese"),
    "AUTONOMOUS long-horizon loop": ("basic_term", "chinese"),
}
GLOSSARY_ROW = re.compile(r"^\| (.+?) \| (.+?) \|$", re.MULTILINE)


def squeeze(text: str) -> str:
    return re.sub(r"\s+", "", text)


def glossary_rows(prompt: str) -> list[tuple[str, str]]:
    """``(source, target)`` rows of the glossary tables in a translation prompt."""
    if "## Glossary Tables" not in prompt:
        return []
    tables = prompt.split("## Glossary Tables", 1)[1].split("## Here is the input:", 1)[0]
    return [row for row in GLOSSARY_ROW.findall(tables) if row != ("Source Term", "Target Term")]


def batch_inputs(request) -> str:
    return "\n".join(item["input"] for item in batch_items(request.prompt) or [])


@pytest.fixture(scope="module")
def finished_task(tmp_path_factory):
    with FakeModel() as model:
        model.respond("term_list", lambda body: json.dumps(MODEL_TERMS, ensure_ascii=False))
        service = launch_service(model, tmp_path_factory.mktemp("terms"))
        try:
            task_id = service.submit(TWO_PAGES)
            polls = []
            final = service.wait(task_id, on_poll=polls.append)
            response = service.http.get(f"/tasks/{task_id}/files/terms")
            report = service.report(task_id)
        finally:
            stop_service(service)
    assert final["state"] == "succeeded", final["error"]
    return {
        "model": model,
        "polls": polls,
        "final": final,
        "terms_response": response,
        "report": report,
    }


def test_term_list_is_requested_once_with_the_dossier_and_the_full_text(finished_task):
    model = finished_task["model"]
    [request] = model.requests_of("term_list")

    sent = squeeze(request.prompt)
    assert squeeze(FAKE_DOSSIER["paper_summary_zh"]) in sent
    assert squeeze(FAKE_DOSSIER["translation_pitfalls_zh"][0]) in sent
    with pymupdf.open(TWO_PAGES) as doc:
        for page in doc:
            assert squeeze(page.get_text()) in sent, f"page {page.number + 1} missing"
    kinds = [r.kind for r in model.requests]
    assert kinds.index("dossier") < kinds.index("term_list") < kinds.index("translate_batch")


def test_term_list_instructions_state_general_rules_with_the_three_examples(finished_task):
    [request] = finished_task["model"].requests_of("term_list")
    instructions = request.body["messages"][0]
    assert instructions["role"] == "system"

    for example in ("long-horizon", "agentic", "recursive self-improvement"):
        assert example in instructions["content"]
    # Nothing from this paper is written into the rules themselves.
    for paper_word in ("AutoDesign", "harness", "poster", "PosterBench"):
        assert paper_word.lower() not in instructions["content"].lower()


def test_terms_file_is_listed_and_downloads_as_json_with_the_spec_fields(finished_task):
    assert "terms" in finished_task["final"]["files"]
    response = finished_task["terms_response"]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")

    entries = response.json()["terms"]
    by_first = {entry["english"][0]: entry for entry in entries}
    assert set(by_first) == set(KEPT)
    for first, (category, treatment) in KEPT.items():
        entry = by_first[first]
        assert (entry["category"], entry["treatment"]) == (category, treatment)
        assert entry["reason"]
        if category == "key_concept":
            assert entry["target"] == first
            assert entry["gloss_zh"]
        else:
            assert entry["gloss_zh"] is None
    assert by_first["code agent"]["target"] == "代码智能体"


def test_entries_not_in_the_source_are_dropped_and_reported(finished_task):
    entries = finished_task["terms_response"].json()["terms"]
    spellings = {s for entry in entries for s in entry["english"]}
    assert INVENTED_KEY not in spellings
    assert INVENTED_BASIC not in spellings
    assert INVENTED_SPELLING not in spellings
    assert "recursive self-improvement" in spellings

    discarded = finished_task["report"]["terms"]["discarded"]
    reported = {s for item in discarded for s in item["english"]}
    assert reported == {INVENTED_KEY, INVENTED_BASIC, INVENTED_SPELLING}


def test_key_concepts_are_injected_as_themselves_and_basic_terms_in_chinese(finished_task):
    batches = finished_task["model"].requests_of("translate_batch")
    # The engine joins a word broken after a hyphen at a line end with a space.
    rows_by_batch = [
        (batch_inputs(r).lower().replace("- ", "-"), glossary_rows(r.prompt)) for r in batches
    ]
    all_rows = {row for _, rows in rows_by_batch for row in rows}

    assert ("meta-harness", "meta-harness") in all_rows
    assert ("Agent-as-a-Judge", "Agent-as-a-Judge") in all_rows
    assert ("recursive self-improvement", "recursive self-improvement") in all_rows
    assert ("code agent", "代码智能体") in all_rows
    for inputs, rows in rows_by_batch:
        for source, _ in rows:
            assert source.lower() in inputs, f"{source} injected into a batch without it"
    injected = {source for source, _ in all_rows}
    assert not injected & {INVENTED_KEY, INVENTED_BASIC, INVENTED_SPELLING}


def test_batches_with_key_concepts_say_that_they_stay_in_english(finished_task):
    batches = [
        r
        for r in finished_task["model"].requests_of("translate_batch")
        if ("meta-harness", "meta-harness") in glossary_rows(r.prompt)
    ]
    assert batches

    for request in batches:
        assert "keep it in English" in request.prompt


def test_status_passes_through_the_term_list_stage(finished_task):
    stages = [status["stage"] for status in finished_task["polls"]]
    assert TERM_STAGE in stages
    assert stages.index(TERM_STAGE) < stages.index("解析版面")


def test_term_list_without_the_spec_fields_is_retried_then_fails_the_task(
    fake_model, start_service
):
    no_gloss = {"terms": [term(["meta-harness"], "key_concept")]}
    fake_model.respond("term_list", lambda body: json.dumps(no_gloss, ensure_ascii=False))
    service = start_service(fake_model)

    final = service.wait_first_failure(service.submit())

    assert final["state"] == "waiting_rerun"  # the attempt failed; retryable (M3 ticket 05)
    assert "本篇术语清单" in final["error"], final["error"]
    assert len(fake_model.requests_of("term_list")) == 4  # first attempt + 3 retries
    assert fake_model.requests_of("translate_batch") == []
