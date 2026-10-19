"""First-mention glosses: where each key concept's gloss is asked for.

Spec "计划覆盖的行为" 7: for every key concept, the first-mention gloss
field appears exactly once in the abstract and exactly once in the body,
each time on the paragraph where the concept first occurs there. Figure
captions do not count as first mentions. A paper without a recognisable
abstract gets the gloss only at the first mention in the body.
"""

import json
import re

import pymupdf
import pytest

from tests.conftest import FIXTURES
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FakeModel
from tests.fake_model import batch_items

# MaLiang-Harness page 1: paper title, authors, an "Abstract" heading, the
# abstract, "1 Introduction" and two introduction paragraphs (split by the
# arXiv stamp), a footnote. The abstract breaks "inspec-\ntion" at a line
# end and writes "MALIANG-HARNESS" in capitals.
MALIANG = FIXTURES / "maliang-p1.pdf"
MALIANG_ABSTRACT = "Executable programs offer"
MALIANG_INTRO_1 = "Over the past decade"
MALIANG_INTRO_2 = "flow-based models"

# AutoDesign pages 1 and 16. Page 1 has no "Abstract" heading: the abstract
# is the long paragraph after the authors. Figure captions on page 1 name
# AutoDesign, DesignHarness and the design harness. Page 16 has the
# heading "8 Conclusion" and its paragraph.
AUTODESIGN = FIXTURES / "autodesign-p1-p16.pdf"
AUTODESIGN_ABSTRACT = "Transforming multimodal sources"
AUTODESIGN_CONCLUSION = "AutoDesign turns recurring"
GLOSS_FIELD = "first_mention_glosses"
FORMAT_EXAMPLE = "（"


def key_concept(english, gloss):
    return {
        "english": english,
        "category": "key_concept",
        "chinese": None,
        "gloss_zh": gloss,
        "reason": "测试依据。",
    }


def basic_term(english, chinese):
    return {
        "english": english,
        "category": "basic_term",
        "chinese": chinese,
        "gloss_zh": None,
        "reason": "测试依据。",
    }


def run_task(tmp_path_factory, pdf, terms):
    """Run one paper task on ``pdf`` with ``terms``; return the batch requests and report."""
    with FakeModel() as model:
        model.respond("term_list", lambda body: json.dumps({"terms": terms}, ensure_ascii=False))
        service = launch_service(model, tmp_path_factory.mktemp("gloss"))
        try:
            task_id = service.submit(pdf)
            final = service.wait(task_id)
            report = service.report(task_id)
        finally:
            stop_service(service)
    assert final["state"] == "succeeded", final["error"]
    return model.requests_of("translate_batch"), report


def gloss_requests(batches) -> dict[str, list[tuple[str, str]]]:
    """Gloss -> ``[(term, paragraph input)]`` for every item that asks for it."""
    found: dict[str, list[tuple[str, str]]] = {}
    for request in batches:
        for item in batch_items(request.prompt) or []:
            for entry in item.get(GLOSS_FIELD, []):
                found.setdefault(entry["gloss_zh"], []).append((entry["term"], item["input"]))
    return found


def plain(text: str) -> str:
    """Paragraph input without style tags, spaces squeezed, line-end hyphens joined."""
    text = re.sub(r"</?style[^>]*>", " ", text)
    return re.sub(r"\s+", " ", text).replace("- ", "-").strip()


def where(requests: list[tuple[str, str]], markers: list[str]) -> list[str]:
    """For each request, the marker whose paragraph it is on (or the input itself)."""
    return [next((m for m in markers if m in plain(text)), text[:60]) for _, text in requests]


MALIANG_TERMS = [
    key_concept(["MaLiang-Harness"], "本文的可编程视觉生成框架"),
    key_concept(["MLLMs", "MLLM"], "多模态大语言模型"),
    key_concept(["inspection"], "检查渲染结果"),
    key_concept(["programmable"], "以程序表达生成过程的"),
    # A gloss ending in a full stop: "（…。）" reads badly, the stop is dropped.
    key_concept(["direct visual synthesis"], "直接合成像素的生成方式。"),
    key_concept(["flow-based models"], "基于流的生成模型"),
    basic_term(["rendering backends"], "渲染后端"),
]


@pytest.fixture(scope="module")
def maliang(tmp_path_factory):
    return run_task(tmp_path_factory, MALIANG, MALIANG_TERMS)


def test_each_key_concept_is_glossed_once_in_the_abstract_and_once_in_the_body(maliang):
    batches, _ = maliang
    found = gloss_requests(batches)
    markers = [MALIANG_ABSTRACT, MALIANG_INTRO_1, MALIANG_INTRO_2]

    # In the abstract and in the introduction.
    assert where(found["以程序表达生成过程的"], markers) == [MALIANG_ABSTRACT, MALIANG_INTRO_2]
    # Only in the abstract: the introduction has "(MLLMs)" only inside
    # parentheses (ticket 15), capitals, and a word broken at a line end.
    assert where(found["多模态大语言模型"], markers) == [MALIANG_ABSTRACT]
    assert where(found["本文的可编程视觉生成框架"], markers) == [MALIANG_ABSTRACT]
    assert where(found["检查渲染结果"], markers) == [MALIANG_ABSTRACT]
    # Only in the body, each on its own paragraph.
    assert where(found["直接合成像素的生成方式"], markers) == [MALIANG_INTRO_1]
    assert where(found["基于流的生成模型"], markers) == [MALIANG_INTRO_2]
    assert len(found) == 6  # basic terms get no gloss


def test_the_gloss_field_names_the_spelling_from_the_term_list(maliang):
    found = gloss_requests(maliang[0])
    assert {term for term, _ in found["本文的可编程视觉生成框架"]} == {"MaLiang-Harness"}
    assert {term for term, _ in found["检查渲染结果"]} == {"inspection"}


def has_glosses(request) -> bool:
    return any(GLOSS_FIELD in item for item in batch_items(request.prompt) or [])


def test_batches_with_glosses_state_the_gloss_format(maliang):
    with_glosses = [r for r in maliang[0] if has_glosses(r)]
    assert with_glosses

    for request in with_glosses:
        instructions = request.prompt.split("## Here is the input:", 1)[0]
        assert "full-width parentheses" in instructions
        assert FORMAT_EXAMPLE in instructions


def test_the_task_report_lists_the_pages_of_the_glosses(maliang):
    _, report = maliang
    by_concept = {item["concept"]: item for item in report["first_mentions"]}
    assert pages_of(by_concept["programmable"]) == (1, 1)
    assert by_concept["MaLiang-Harness"]["body_page"] is None
    assert by_concept["direct visual synthesis"]["abstract_page"] is None
    assert len(by_concept) == 6


AUTODESIGN_TERMS = [
    key_concept(["AutoDesign"], "本文提出的自动设计框架"),
    key_concept(["DesignHarness"], "学到的设计框架"),
    key_concept(["PosterBench"], "本文的海报评测基准"),
    key_concept(["long-horizon"], "需要长步骤持续执行的"),
    # Only in figure captions on page 1.
    key_concept(["design harness"], "生成设计的框架"),
    # "harness" inside "meta-harness" is not a mention of "harness".
    key_concept(["meta-harness"], "优化框架本身的外层框架"),
    key_concept(["harness"], "包裹模型的执行框架"),
]


@pytest.fixture(scope="module")
def autodesign(tmp_path_factory):
    return run_task(tmp_path_factory, AUTODESIGN, AUTODESIGN_TERMS)


def test_without_an_abstract_heading_the_long_paragraph_after_the_title_is_the_abstract(
    autodesign,
):
    found = gloss_requests(autodesign[0])
    markers = [AUTODESIGN_ABSTRACT, AUTODESIGN_CONCLUSION]

    # Figure 1's captions name AutoDesign and DesignHarness before page 16.
    assert where(found["本文提出的自动设计框架"], markers) == markers
    assert where(found["学到的设计框架"], markers) == markers
    assert where(found["本文的海报评测基准"], markers) == markers
    assert where(found["需要长步骤持续执行的"], markers) == [AUTODESIGN_ABSTRACT]
    assert where(found["优化框架本身的外层框架"], markers) == [AUTODESIGN_ABSTRACT]
    assert where(found["包裹模型的执行框架"], markers) == [AUTODESIGN_ABSTRACT]


def test_figure_captions_are_not_first_mentions(autodesign):
    found = gloss_requests(autodesign[0])
    assert "生成设计的框架" not in found
    for requests in found.values():
        for _, text in requests:
            assert not text.startswith(("Figure", "(a)", "(b)"))


def test_batches_without_glosses_do_not_carry_the_gloss_rules(autodesign):
    without = [r for r in autodesign[0] if not has_glosses(r)]
    assert without
    for request in without:
        assert "full-width parentheses" not in request.prompt


def test_without_a_recognisable_abstract_only_the_body_is_glossed(tmp_path_factory):
    # AutoDesign page 16 alone: no paper title, no abstract. The page starts
    # inside "7 Related Work" and has the heading "8 Conclusion".
    page16 = tmp_path_factory.mktemp("page16") / "autodesign-p16.pdf"
    with pymupdf.open(AUTODESIGN) as source, pymupdf.open() as excerpt:
        excerpt.insert_pdf(source, from_page=1, to_page=1)
        excerpt.save(page16)
    batches, report = run_task(tmp_path_factory, page16, AUTODESIGN_TERMS)

    found = gloss_requests(batches)
    autodesign_first = "direct algorithms for AutoDesign"
    posterbench_first = "final DesignHarness with PosterBench"
    markers = [autodesign_first, posterbench_first, AUTODESIGN_CONCLUSION]
    assert where(found["本文提出的自动设计框架"], markers) == [autodesign_first]
    assert where(found["本文的海报评测基准"], markers) == [posterbench_first]
    pages = {item["concept"]: item for item in report["first_mentions"]}
    assert pages_of(pages["AutoDesign"]) == (None, 1)


def pages_of(placement: dict) -> tuple[int | None, int | None]:
    """The abstract and body pages of a concept's glosses in the task report."""
    return placement["abstract_page"], placement["body_page"]
