"""Glosses that say what a concept is, and one Chinese name per key concept.

Ticket 18 (spec 5 "本篇术语清单的条目字段" and "释义与中文指称名的核对"):

- The term list instructions ask for a gloss that says what kind of thing
  the concept is and what it does in this paper, not a literal rendering,
  with 15 characters as a hard limit; and for each key concept a Chinese
  name (``name_zh``), the head noun of its own gloss, which other glosses
  and the Chinese renderings of basic terms containing the concept use.
- After the source check and the 20 limit, the paper task checks the
  glosses and the names and writes what it finds into ``terms.gloss_checks``
  of the task report: glosses over 15 characters (whitespace not counted),
  compound key concepts whose gloss lacks the name of a key concept they
  contain, basic terms whose Chinese rendering lacks it, and key concepts
  without a name. Containment compares spellings lower-cased, without
  hyphens and spaces. Nothing is retried, rewritten or cut.
"""

import json

import pytest

from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FakeModel
from tests.test_term_list import glossary_rows
from tests.test_term_list import term

# 23 characters: over the limit.
LONG_GLOSS = "让代码智能体依据反馈反复改进外围程序的设计框架"
# 15 characters and one space: within the limit.
SPACED_GLOSS = "论文转海报 生成的多学科评测基准"
META_GLOSS = "跨任务改进测试床的优化系统"
AGENTIC_GLOSS = "自主规划与执行的能力"


def key(english, gloss_zh, name_zh):
    return {**term(english, "key_concept", gloss_zh=gloss_zh), "name_zh": name_zh}


# Every spelling appears on page 1 of AutoDesign (the test excerpt).
MODEL_TERMS = {
    "terms": [
        key(["harness"], "编排冻结模型的外围程序", "外围程序"),
        # Compound of harness; its gloss lacks 外围程序.
        key(["meta-harness", "Meta-Harness"], META_GLOSS, "优化系统"),
        # Compound of harness (case and missing hyphen ignored); its gloss has 外围程序.
        key(["DesignHarness"], "面向设计任务的外围程序", "外围程序"),
        key(["AutoDesign"], LONG_GLOSS, "设计框架"),
        key(["PosterBench"], SPACED_GLOSS, "评测基准"),
        # No name: reported, and the checks that need it are skipped.
        key(["agentic"], AGENTIC_GLOSS, None),
        term(["harness system"], "basic_term", chinese="测试床系统"),
        term(["model-harness system"], "basic_term", chinese="模型与外围程序系统"),
        # Contains harness (name present) and meta-harness (name absent).
        term(["meta-harness optimizer"], "basic_term", chinese="元外围程序优化器"),
        term(["agentic process"], "basic_term", chinese="智能体流程"),
    ]
}


@pytest.fixture(scope="module")
def finished_task(tmp_path_factory):
    with FakeModel() as model:
        model.respond("term_list", lambda body: json.dumps(MODEL_TERMS, ensure_ascii=False))
        service = launch_service(model, tmp_path_factory.mktemp("names"))
        try:
            task_id = service.submit()
            final = service.wait(task_id)
            terms = service.http.get(f"/tasks/{task_id}/files/terms").json()["terms"]
            report = service.report(task_id)
        finally:
            stop_service(service)
    assert final["state"] == "succeeded", final["error"]
    return {"model": model, "terms": terms, "checks": report["terms"]["gloss_checks"]}


def instructions(model: FakeModel) -> str:
    [request] = model.requests_of("term_list")
    return " ".join(request.body["messages"][0]["content"].split())


def by_english(terms: list[dict]) -> dict:
    return {t["english"][0]: t for t in terms}


def test_instructions_ask_for_a_gloss_that_says_what_the_concept_is(finished_task):
    text = instructions(finished_task["model"])
    assert "what kind of thing it is" in text
    assert "what it does or what role it plays in this paper" in text
    assert "It is not a literal, word-for-word rendering of the English term" in text
    assert "a dictionary equivalent of one or two words is not a gloss" in text
    assert "15 Chinese characters is a hard limit" in text
    assert "delete modifiers and keep the head noun" in text
    assert "never ends with 的" in text


def test_instructions_ask_for_one_chinese_name_per_key_concept(finished_task):
    text = instructions(finished_task["model"])
    assert '"name_zh"' in text
    assert "the head noun of its own gloss" in text
    assert "uses this name exactly as written" in text
    assert "Chinese rendering of a basic term whose English spelling contains" in text


def test_names_are_saved_in_the_term_list(finished_task):
    terms = by_english(finished_task["terms"])
    assert terms["harness"]["name_zh"] == "外围程序"
    assert terms["meta-harness"]["name_zh"] == "优化系统"
    assert terms["agentic"]["name_zh"] is None
    assert terms["harness system"]["name_zh"] is None


def test_a_missing_name_is_reported_and_not_retried(finished_task):
    assert len(finished_task["model"].requests_of("term_list")) == 1
    assert finished_task["checks"]["key_concepts_without_name"] == [
        {"english": ["agentic"], "gloss_zh": AGENTIC_GLOSS}
    ]


def test_a_gloss_over_15_characters_is_reported_and_kept_whole(finished_task):
    assert finished_task["checks"]["long_glosses"] == [
        {"english": ["AutoDesign"], "name_zh": "设计框架", "gloss_zh": LONG_GLOSS, "characters": 23}
    ]
    terms = by_english(finished_task["terms"])
    assert terms["AutoDesign"]["gloss_zh"] == LONG_GLOSS
    assert terms["PosterBench"]["gloss_zh"] == SPACED_GLOSS


def test_a_compound_gloss_without_the_contained_name_is_reported(finished_task):
    assert finished_task["checks"]["compound_glosses_without_name"] == [
        {
            "english": ["meta-harness", "Meta-Harness"],
            "gloss_zh": META_GLOSS,
            "contains": {"english": ["harness"], "name_zh": "外围程序"},
        }
    ]


def test_a_basic_term_without_the_contained_name_is_reported(finished_task):
    assert finished_task["checks"]["basic_terms_without_name"] == [
        {
            "english": ["harness system"],
            "chinese": "测试床系统",
            "contains": {"english": ["harness"], "name_zh": "外围程序"},
        },
        {
            "english": ["meta-harness optimizer"],
            "chinese": "元外围程序优化器",
            "contains": {"english": ["meta-harness", "Meta-Harness"], "name_zh": "优化系统"},
        },
    ]


def test_names_do_not_enter_the_glossary(finished_task):
    batches = finished_task["model"].requests_of("translate_batch")
    rows = [row for r in batches for row in glossary_rows(r.prompt)]
    assert ("harness", "harness") in rows
    names = {t["name_zh"] for t in finished_task["terms"] if t.get("name_zh")}
    assert not {target for _, target in rows} & names
