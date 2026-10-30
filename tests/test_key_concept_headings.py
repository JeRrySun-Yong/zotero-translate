"""Section headings that are a key concept's full name are translated into Chinese (closure ticket 17).

Requirement 1 keeps key concepts in English. The user decided on
2026-10-29 that a section heading that is a key concept's full name is
translated into Chinese, in Chinese only (AutoRef page 5: "5.1
REFERENCE-GROUNDED PROMPTING" to "5.4 FAILURE-AWARE SELECTION" came back in
English in C-10). Body text, figures, tables and algorithms keep key
concepts in English as before.

- A heading item (layout label "title" or "paragraph_title") whose text,
  after its section number, is one spelling of a key concept with at least
  two words gets "key_concept_heading" (the spelling) in the batch request,
  and the request states the rule; batches without such a heading are
  unchanged. A one-word name ("4 AUTOREF") is no full name and stays as it is.
- The review request carries the field and the rule, so the review does not
  put the English back.
- Headings are still no first-mention positions.
"""

import json

import pytest

from tests.conftest import FIXTURES
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FakeModel
from tests.fake_model import batch_items
from tests.fake_model import review_items
from tests.test_first_mention_gloss import GLOSS_FIELD
from tests.test_first_mention_gloss import key_concept
from zotero_translate.first_mention import FirstMentionPlan

# AutoRef page 5, taken with PyMuPDF from the full source (SHA-256
# 31ad6c5e…, requirements.md): the four component headings 5.1-5.4 in small
# capitals, each followed by its paragraph, then "6 EXPERIMENTS" and "6.1
# EXPERIMENTAL SETTINGS". The headings' spellings occur nowhere else on the
# page; "AutoRef-Harness" occurs in the paragraph under 5.1.
AUTOREF_P5 = FIXTURES / "autoref-p5.pdf"
# Heading -> the field's value: the first of the concept's spellings it matches.
HEADINGS = {
    "5.1 REFERENCE-GROUNDED PROMPTING": "reference-grounded prompting",
    "5.2 STRUCTURALLY DIVERSE DRAFTS": "structurally diverse drafts",
    "5.3 COMPLAINT-DIRECTED REVISION": "complaint-directed revision",
    "5.4 FAILURE-AWARE SELECTION": "failure-aware selection",
}
OTHER_HEADINGS = ["6 EXPERIMENTS", "6.1 EXPERIMENTAL SETTINGS"]
TERMS = [
    key_concept(["reference-grounded prompting", "Reference-Grounded Prompting"], "将请求绑定到参考图的提示机制"),
    key_concept(["structurally diverse drafts", "Structurally Diverse Drafts"], "用不同提示结构生成草稿的机制"),
    key_concept(["complaint-directed revision", "Complaint-Directed Revision"], "针对缺陷改写提示词的机制"),
    key_concept(["failure-aware selection", "Failure-Aware Selection"], "先排查硬性缺陷的筛选机制"),
    key_concept(["AutoRef-Harness"], "搜索出的最优外围程序"),
]
FIELD = "key_concept_heading"

# Phrases of the rules, as the requests state them.
TRANSLATION_RULE = [
    "a section heading that is the full name of a key concept of this paper",
    "write it in Chinese only",
    "This overrides, for these items only, the glossary rule that keeps key concepts in English",
]
REVIEW_RULE = [
    "a section heading that is the full name of a key concept",
    "in Chinese only",
    "do not put the English back",
]
KEEP_ENGLISH = "keep it in English exactly as written, never translate it"


@pytest.fixture(scope="module")
def autoref_p5(tmp_path_factory):
    with FakeModel() as model:
        model.respond("term_list", lambda body: json.dumps({"terms": TERMS}, ensure_ascii=False))
        service = launch_service(model, tmp_path_factory.mktemp("headings"))
        try:
            task_id = service.submit(AUTOREF_P5)
            final = service.wait(task_id)
            report = service.report(task_id)
        finally:
            stop_service(service)
    assert final["state"] == "succeeded", final["error"]
    return model, report


def items_by_input(requests, items_of, key):
    """Source text -> (item, request prompt) for every item of ``requests``."""
    return {
        item[key]: (item, " ".join(request.prompt.split()))
        for request in requests
        for item in items_of(request.prompt) or []
    }


def test_a_heading_that_is_a_key_concept_is_asked_for_in_chinese(autoref_p5):
    model, _ = autoref_p5
    found = items_by_input(model.requests_of("translate_batch"), batch_items, "input")
    for heading, spelling in HEADINGS.items():
        item, prompt = found[heading]
        assert item[FIELD] == spelling
        for phrase in TRANSLATION_RULE:
            assert phrase in prompt, (heading, phrase)
        # The glossary rule is still there, for the other items.
        assert KEEP_ENGLISH in prompt


def test_other_headings_and_batches_are_unchanged(autoref_p5):
    model, _ = autoref_p5
    batches = model.requests_of("translate_batch")
    found = items_by_input(batches, batch_items, "input")
    for heading in OTHER_HEADINGS:
        assert FIELD not in found[heading][0]
    without = [r for r in batches if not any(FIELD in i for i in batch_items(r.prompt))]
    assert without
    for request in without:
        assert "## Section Headings" not in request.prompt
        assert TRANSLATION_RULE[1] not in " ".join(request.prompt.split())


def test_the_review_keeps_the_heading_in_chinese(autoref_p5):
    model, _ = autoref_p5
    found = items_by_input(model.requests_of("review"), review_items, "source")
    for heading, spelling in HEADINGS.items():
        item, prompt = found[heading]
        assert item[FIELD] == spelling
        for phrase in REVIEW_RULE:
            assert phrase in prompt, (heading, phrase)
    for request in model.requests_of("review"):
        if not any(FIELD in i for i in review_items(request.prompt)):
            assert REVIEW_RULE[2] not in " ".join(request.prompt.split())


def test_headings_are_still_no_first_mentions(autoref_p5):
    model, report = autoref_p5
    found = items_by_input(model.requests_of("translate_batch"), batch_items, "input")
    for heading in HEADINGS:
        assert GLOSS_FIELD not in found[heading][0]
    pages = {p["concept"]: (p["abstract_page"], p["body_page"]) for p in report["first_mentions"]}
    for concept in ["reference-grounded prompting", "structurally diverse drafts"]:
        assert pages[concept] == (None, None)
    assert pages["AutoRef-Harness"] == (None, 1)


def test_only_a_heading_that_is_a_key_concept_of_several_words_counts():
    plan = FirstMentionPlan(
        [
            key_concept(["reference-grounded prompting"], "提示机制"),
            key_concept(
                ["Traceable Generation Process (TGP)", "Traceable Generation Process", "TGP"],
                "关联编辑与渲染证据的机制",
            ),
            key_concept(["AutoRef", "AUTOREF"], "搜索外围程序的方法"),
            key_concept(["AutoRef-Harness"], "搜索出的最优外围程序"),
        ]
    )
    paragraphs = [
        (4, "title", "5.1 REFERENCE-GROUNDED PROMPTING"),
        (6, "paragraph_title", "3.2 Traceable Generation Process"),
        # A name of one word has no Chinese full name.
        (2, "title", "4 AUTOREF"),
        (3, "title", "5 THE OPTIMIZED AUTOREF-HARNESS"),
        (6, "title", "TGP"),
        # More than the key concept.
        (6, "title", "6.3 Transferability of Reference-Grounded Prompting"),
        # Not a heading.
        (4, "plain text", "Reference-Grounded Prompting"),
        # No section number, a final full stop, a plural.
        (4, "title", "Reference-grounded promptings."),
    ]
    assert plan.key_concept_headings(paragraphs) == {
        0: "reference-grounded prompting",
        1: "Traceable Generation Process",
        7: "reference-grounded prompting",
    }


def test_a_section_number_has_a_digit_or_a_full_stop():
    plan = FirstMentionPlan(
        [
            key_concept(["reference-grounded prompting"], "提示机制"),
            key_concept(["survey of harness optimization"], "外围程序优化综述"),
        ]
    )
    paragraphs = [
        # "A" is an article here, not a section number.
        (0, "title", "A Survey of Harness Optimization"),
        (0, "title", "G.2 Reference-Grounded Prompting"),
        (0, "title", "IV. Reference-Grounded Prompting"),
        (0, "title", "B. Reference-Grounded Prompting"),
        (0, "title", "3. Reference-Grounded Prompting"),
    ]
    assert plan.key_concept_headings(paragraphs) == {
        1: "reference-grounded prompting",
        2: "reference-grounded prompting",
        3: "reference-grounded prompting",
        4: "reference-grounded prompting",
    }
