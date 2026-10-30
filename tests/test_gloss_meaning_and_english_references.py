"""Glosses that say what a concept is, English references, and the three gloss checks.

Ticket 18 (spec 5 "本篇术语清单的条目字段"): the term list instructions ask
for a gloss that says what kind of thing the concept is and what it does in
this paper, not a literal rendering, with 15 Chinese characters as a hard
limit (closure-01: the instructions state 12, because argon goes over the
stated limit; the check (c) stays at 15).

Ticket 20 (spec 5, 2026-10-20 second set of decisions): the ticket 18
Chinese name is gone. A gloss that mentions another key concept writes that
concept's English spelling, at most one; a basic term whose spelling
contains a key concept keeps that spelling in its rendering. After the term
list is final, the paper task writes three checks into ``terms.gloss_checks``
of the task report, and nothing is retried, rewritten or cut:

- (a) ``latin_not_key_concept``: a Latin string in a gloss that is no key
  concept's spelling (case ignored);
- (b) ``basic_terms_without_spelling``: a basic term that contains a key
  concept as whole words, whose rendering does not contain a spelling of it
  as whole words;
- (c) ``long_glosses``: a gloss over 15 Chinese characters; embedded
  English does not count.

Whole words: spellings are split into lower-cased tokens at
non-alphanumeric characters and camel case; REV is not contained in
revision.
"""

import json

import pytest

from tests.conftest import FIXTURES
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FakeModel
from tests.test_term_list import glossary_rows
from tests.test_term_list import term

MALIANG_P1 = FIXTURES / "maliang-p1.pdf"

# 23 Chinese characters: over the limit.
LONG_GLOSS = "让代码智能体依据反馈反复改进外围程序的设计框架"
# 15 Chinese characters and an embedded English spelling: within the limit.
EMBEDDED_GLOSS = "面向设计任务并由 meta-harness 学得的外围程序"
# A Latin string that is no key concept's spelling.
FOREIGN_LATIN_GLOSS = "评测海报生成的 benchmark 数据集"
# A key concept's spelling in another case than listed: accepted.
CASED_GLOSS = "跨任务改进 HARNESS 的优化系统"


def key(english, gloss_zh):
    return term(english, "key_concept", gloss_zh=gloss_zh)


# Every spelling appears on page 1 of AutoDesign (the test excerpt).
AUTODESIGN_TERMS = {
    "terms": [
        key(["harness"], "编排冻结模型的外围程序"),
        key(["meta-harness", "Meta-Harness"], CASED_GLOSS),
        key(["DesignHarness"], EMBEDDED_GLOSS),
        key(["AutoDesign"], LONG_GLOSS),
        key(["PosterBench"], FOREIGN_LATIN_GLOSS),
        # (b): contains harness; the rendering lacks it.
        term(["harness system"], "basic_term", chinese="测试床系统"),
        # Contains harness; the rendering keeps it.
        term(["model-harness system"], "basic_term", chinese="模型-harness 系统"),
        # (b): contains harness (kept) and meta-harness (not kept): one record.
        term(["meta-harness optimizer"], "basic_term", chinese="元 harness 优化器"),
    ]
}

# Every spelling appears on page 1 of MaLiang-Harness.
MALIANG_TERMS = {
    "terms": [
        key(["Revision-aware Editing and Verification", "REV"], "回滚并核对修订的机制"),
        key(["MaLiang-Harness"], "以 REV 支撑修订的生成框架"),
        # Contains REV only as a string, not as whole words: no (b) record.
        term(["revision"], "basic_term", chinese="修订"),
        term(["revision reference"], "basic_term", chinese="修订参照"),
    ]
}


def run_task(tmp_path_factory, terms: dict, pdf=None) -> dict:
    with FakeModel() as model:
        model.respond("term_list", lambda body: json.dumps(terms, ensure_ascii=False))
        service = launch_service(model, tmp_path_factory.mktemp("glosses"))
        try:
            task_id = service.submit(pdf) if pdf else service.submit()
            final = service.wait(task_id)
            saved = service.http.get(f"/tasks/{task_id}/files/terms").json()["terms"]
            report = service.report(task_id)
        finally:
            stop_service(service)
    assert final["state"] == "succeeded", final["error"]
    return {"model": model, "terms": saved, "checks": report["terms"]["gloss_checks"], "report": report}


@pytest.fixture(scope="module")
def autodesign(tmp_path_factory):
    return run_task(tmp_path_factory, AUTODESIGN_TERMS)


@pytest.fixture(scope="module")
def maliang(tmp_path_factory):
    return run_task(tmp_path_factory, MALIANG_TERMS, MALIANG_P1)


def instructions(model: FakeModel) -> str:
    [request] = model.requests_of("term_list")
    return " ".join(request.body["messages"][0]["content"].split())


def by_english(terms: list[dict]) -> dict:
    return {t["english"][0]: t for t in terms}


def test_instructions_ask_for_a_gloss_that_says_what_the_concept_is(autodesign):
    text = instructions(autodesign["model"])
    assert "what kind of thing it is" in text
    assert "what it does or what role it plays in this paper" in text
    assert "It is not a literal, word-for-word rendering of the English term" in text
    assert "a dictionary equivalent of one or two words is not a gloss" in text
    assert "12 Chinese characters is a hard limit" in text
    assert "delete modifiers and keep the head noun" in text
    assert "never ends with 的" in text


def test_instructions_ask_for_english_references_not_chinese_names(autodesign):
    text = instructions(autodesign["model"])
    assert "name_zh" not in text
    assert "it writes that concept's English spelling exactly as listed" in text
    assert "a gloss contains at most one such English spelling" in text
    assert "never repeats the English spelling of its own concept" in text
    assert "an embedded English spelling does not count towards it" in text
    assert "its Chinese rendering keeps that key concept's English spelling" in text


def test_the_term_list_has_no_chinese_name(autodesign):
    assert all("name_zh" not in t for t in autodesign["terms"])


def test_checks_are_reported_and_not_retried(autodesign):
    assert len(autodesign["model"].requests_of("term_list")) == 1
    assert autodesign["model"].requests_of("promoted_glosses") == []
    assert set(autodesign["checks"]) == {
        "latin_not_key_concept",
        "basic_terms_without_spelling",
        "long_glosses",
    }


def test_a_latin_string_that_is_no_key_concept_is_reported(autodesign):
    assert autodesign["checks"]["latin_not_key_concept"] == [
        {"english": ["PosterBench"], "gloss_zh": FOREIGN_LATIN_GLOSS, "latin": "benchmark"}
    ]


def test_a_basic_term_whose_rendering_lacks_the_contained_key_concept_is_reported(autodesign):
    assert autodesign["checks"]["basic_terms_without_spelling"] == [
        {"english": ["harness system"], "chinese": "测试床系统", "contains": ["harness"]},
        {
            "english": ["meta-harness optimizer"],
            "chinese": "元 harness 优化器",
            "contains": ["meta-harness", "Meta-Harness"],
        },
    ]


def test_a_gloss_over_15_chinese_characters_is_reported_and_kept_whole(autodesign):
    assert autodesign["checks"]["long_glosses"] == [
        {"english": ["AutoDesign"], "gloss_zh": LONG_GLOSS, "characters": 23}
    ]
    terms = by_english(autodesign["terms"])
    assert terms["AutoDesign"]["gloss_zh"] == LONG_GLOSS
    assert terms["DesignHarness"]["gloss_zh"] == EMBEDDED_GLOSS


def test_containment_is_by_whole_words(maliang):
    """REV is in the gloss and contained in "revision" as a string only: no record."""
    assert maliang["checks"] == {
        "latin_not_key_concept": [],
        "basic_terms_without_spelling": [],
        "long_glosses": [],
    }
    assert maliang["report"]["terms"]["promoted"] == []


def test_key_concepts_stay_english_in_the_glossary(autodesign):
    batches = autodesign["model"].requests_of("translate_batch")
    rows = [row for r in batches for row in glossary_rows(r.prompt)]
    assert ("harness", "harness") in rows
    assert ("model-harness system", "模型-harness 系统") in rows
