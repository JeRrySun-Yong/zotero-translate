"""Basic term promotion and the promoted glosses request.

Ticket 20 (spec 5 "基础术语升级" and "释义补写请求"):

- After the source check and before the 20 limit, a basic term with a
  spelling that the spellings of at least 2 key concepts contain as whole
  words becomes a key concept, in its place; a spelling that ends with "."
  takes no part. The task report lists each promotion with the key concepts
  that contain it.
- When promoted terms remain, one more request with the dossier and the term
  list, but not the full text, writes their glosses and rewrites the glosses
  and basic term renderings that mention them.
- When that request fails for good, the task still succeeds: the promoted
  term stays in English without a gloss, gets no first-mention gloss, and
  the task report says so.
"""

import json

import pytest

from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FAKE_DOSSIER
from tests.fake_model import FakeModel
from tests.fake_model import promoted_glosses_request
from tests.test_term_list import glossary_rows
from tests.test_term_list import term

META_GLOSS = "跨任务改进外围程序的优化系统"
REWRITTEN_META_GLOSS = "跨任务改进 harness 的优化系统"
HARNESS_GLOSS = "编排冻结模型的外围程序"

# Every spelling appears on page 1 of AutoDesign (the test excerpt).
MODEL_TERMS = {
    "terms": [
        term(["AutoDesign"], "key_concept", gloss_zh="自动设计海报的系统"),
        term(["meta-harness", "Meta-Harness"], "key_concept", gloss_zh=META_GLOSS),
        term(["DesignHarness"], "key_concept", gloss_zh="面向设计任务的外围程序"),
        # Contained in exactly 2 key concepts, meta-harness and DesignHarness (camel case): promoted.
        term(["harness"], "basic_term", chinese="测试支架"),
        # Contains harness: its rendering is rewritten.
        term(["harness system"], "basic_term", chinese="外围程序系统"),
        # Contained in one key concept only: not promoted.
        term(["Main Track"], "basic_term", chinese="主赛道"),
        term(["PosterBench Main Track"], "key_concept", gloss_zh="海报生成的主评测集"),
        # Ends with ".": takes no part, though two key concepts contain "system".
        term(["system."], "basic_term", chinese="系统"),
        term(["closed-source commercial system"], "key_concept", gloss_zh="闭源的商用设计系统"),
        term(["system-blind"], "key_concept", gloss_zh="不告知评审者系统身份的评测方式"),
    ]
}

FULL_TEXT_SENTENCE = "Transforming multimodal sources into condensed and structured media outputs"


def rewriting_responder(body: dict) -> str:
    promoted = promoted_glosses_request(body)["promoted"]
    assert [p["english"][0] for p in promoted] == ["harness"]
    return json.dumps(
        {
            "glosses": [
                {"english": "harness", "gloss_zh": HARNESS_GLOSS},
                {"english": "meta-harness", "gloss_zh": REWRITTEN_META_GLOSS},
            ],
            "renderings": [{"english": "harness system", "chinese": "harness 系统"}],
        },
        ensure_ascii=False,
    )


def run_task(tmp_path_factory, promoted_glosses) -> dict:
    with FakeModel() as model:
        model.respond("term_list", lambda body: json.dumps(MODEL_TERMS, ensure_ascii=False))
        model.respond("promoted_glosses", promoted_glosses)
        service = launch_service(model, tmp_path_factory.mktemp("promotion"))
        try:
            task_id = service.submit()
            final = service.wait(task_id)
            saved = service.http.get(f"/tasks/{task_id}/files/terms").json()["terms"]
            report = service.report(task_id)
        finally:
            stop_service(service)
    return {"model": model, "final": final, "terms": {t["english"][0]: t for t in saved}, "report": report}


@pytest.fixture(scope="module")
def rewritten(tmp_path_factory):
    return run_task(tmp_path_factory, rewriting_responder)


@pytest.fixture(scope="module")
def failed(tmp_path_factory):
    return run_task(tmp_path_factory, lambda body: "这不是 JSON")


def test_a_basic_term_contained_in_two_key_concepts_is_promoted(rewritten):
    harness = rewritten["terms"]["harness"]
    assert harness["category"] == "key_concept"
    assert harness["treatment"] == "keep_english"
    assert list(rewritten["terms"]).index("harness") == 3, "the promoted term keeps its place"
    assert rewritten["report"]["terms"]["promoted"] == [
        {
            "english": ["harness"],
            "chinese": "测试支架",
            "contained_in": [["meta-harness", "Meta-Harness"], ["DesignHarness"]],
        }
    ]


def test_a_basic_term_contained_in_one_key_concept_is_not_promoted(rewritten):
    assert rewritten["terms"]["Main Track"]["category"] == "basic_term"


def test_a_spelling_that_ends_with_a_full_stop_takes_no_part(rewritten):
    assert rewritten["terms"]["system."]["category"] == "basic_term"


def test_the_promoted_glosses_request_has_the_dossier_and_the_term_list_but_not_the_full_text(
    rewritten,
):
    [request] = rewritten["model"].requests_of("promoted_glosses")
    content = request.body["messages"][-1]["content"]
    assert FAKE_DOSSIER["paper_summary_zh"] in content
    assert FULL_TEXT_SENTENCE not in content
    sent = promoted_glosses_request(request.body)
    assert sent["promoted"][0]["english"] == ["harness"]
    listed = {t["english"][0]: t for t in sent["terms"]}
    assert listed["harness"] == {"english": ["harness"], "category": "key_concept", "gloss_zh": None}
    assert listed["harness system"]["chinese"] == "外围程序系统"
    assert "测试支架" not in content, "the former rendering is not offered as a gloss"


def test_the_promoted_term_gets_a_gloss_and_mentions_of_it_are_rewritten(rewritten):
    terms = rewritten["terms"]
    assert terms["harness"]["gloss_zh"] == HARNESS_GLOSS
    assert terms["meta-harness"]["gloss_zh"] == REWRITTEN_META_GLOSS
    assert terms["harness system"]["target"] == "harness 系统"
    assert terms["harness system"]["category"] == "basic_term"
    record = rewritten["report"]["terms"]["gloss_request"]
    assert record["succeeded"] is True
    assert record["glosses"] == [{"english": "harness", "gloss_zh": HARNESS_GLOSS}]
    assert record["rewritten"] == [
        {
            "english": ["meta-harness", "Meta-Harness"],
            "field": "gloss_zh",
            "before": META_GLOSS,
            "after": REWRITTEN_META_GLOSS,
        },
        {"english": ["harness system"], "field": "chinese", "before": "外围程序系统", "after": "harness 系统"},
    ]


def test_the_engine_uses_the_rewritten_list(rewritten):
    model = rewritten["model"]
    rows = [row for r in model.requests_of("translate_batch") for row in glossary_rows(r.prompt)]
    assert ("harness", "harness") in rows
    assert ("harness system", "harness 系统") in rows
    # The excerpt has no first-mention position; the plan still lists the concept.
    assert "harness" in {item["concept"] for item in rewritten["report"]["first_mentions"]}


def test_a_failed_promoted_glosses_request_does_not_fail_the_task(failed):
    assert failed["final"]["state"] == "succeeded", failed["final"]["error"]
    # Retried by the pipeline model client's usual rule: 1 + 3 attempts.
    assert len(failed["model"].requests_of("promoted_glosses")) == 4
    harness = failed["terms"]["harness"]
    assert harness["category"] == "key_concept"
    assert harness["gloss_zh"] is None
    assert failed["terms"]["meta-harness"]["gloss_zh"] == META_GLOSS
    assert failed["terms"]["harness system"]["target"] == "外围程序系统"
    record = failed["report"]["terms"]["gloss_request"]
    assert record["succeeded"] is False
    assert record["promoted"] == ["harness"]
    assert "无法解析" in record["error"]


def test_a_key_concept_without_a_gloss_is_skipped(failed):
    report = failed["report"]
    assert "harness" not in {item["concept"] for item in report["first_mentions"]}
    assert "meta-harness" in {item["concept"] for item in report["first_mentions"]}
    rows = [row for r in failed["model"].requests_of("translate_batch") for row in glossary_rows(r.prompt)]
    assert ("harness", "harness") in rows
    # The checks skip it; the basic term that contains it is still checked.
    assert report["terms"]["gloss_checks"]["basic_terms_without_spelling"] == [
        {"english": ["harness system"], "chinese": "外围程序系统", "contains": ["harness"]}
    ]
