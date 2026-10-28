"""Short glosses and their placement (ticket 15, spec 5 "释义格式").

- A key concept's gloss is a Chinese noun phrase of at most 15 characters,
  without parentheses, English or final punctuation. The instructions ask
  for at most 12 (closure-01: argon goes over the stated limit).
- An occurrence inside the source's parentheses ("… models (MLLMs)") is not
  a first mention: the plan takes the next occurrence.
- The translation instructions say where the gloss goes (after the whole
  English phrase, outside style tags), that it may be left out where the
  source explains the concept right there, and that the translation adds
  no English words after Chinese and no parentheses the source lacks.
- The task report records, for every planned gloss, whether the final
  translation contains it (both NFKC-normalised), and lists those it does
  not contain. Nothing is translated again because of it.
"""

import json
import re
import unicodedata

import pymupdf
import pytest

from tests.conftest import FIXTURES
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import FakeModel
from tests.fake_model import _last_user_content
from tests.fake_model import batch_items
from tests.test_first_mention_gloss import GLOSS_FIELD
from tests.test_first_mention_gloss import gloss_requests
from tests.test_first_mention_gloss import key_concept
from tests.test_first_mention_gloss import where

# MaLiang-Harness pages 1 and 3 (page 2 is a large figure, left out), taken
# with PyMuPDF from the full source (SHA-256 c0372d67…, see backlog, ticket
# 10). Page 1: the abstract, then "1 Introduction", whose second piece
# names "multimodal large language models (MLLMs)" in parentheses. Page 3
# goes on with the introduction: "an MLLM selects …" and "Persistent
# Executable Generation (PEG) State".
MALIANG_P1_P3 = FIXTURES / "maliang-p1-p3.pdf"
ABSTRACT = "Executable programs offer"
INTRO_2 = "flow-based models"
PAGE_3_MLLM = "an MLLM selects"

# The gloss of MLLMs contains 行; the fake model writes it as the CJK
# compatibility character U+FA08 that PDF text layers produce.
MLLM_GLOSS = "执行多模态理解的大模型"
TERMS = [
    key_concept(["MLLMs", "MLLM"], MLLM_GLOSS),
    # Only ever inside parentheses: never a first mention.
    key_concept(["PEG"], "持久可执行生成状态"),
    key_concept(["programmable"], "以程序表达生成过程"),
]
# The fake model leaves this gloss out in the body (as when the source
# explains the concept right there).
OMITTED = ("programmable", "以程序表达生成过程")

# Phrases of the rules, as the instructions state them.
OUTSIDE_PARENTHESES = "first occurrence of each listed \"term\" in the item that is not inside parentheses"
GLOSS_RULES = [
    OUTSIDE_PARENTHESES,
    "after the whole English phrase that contains the term",
    "outside style tags",
    "you may leave the gloss out",
]
GENERAL_RULES = [
    "Do not add the English source words after Chinese text",
    "Do not turn the source's commas, dashes or appositives into parentheses",
]
TERM_LIST_GLOSS_RULES = [
    "a Chinese noun phrase of at most 12 Chinese characters",
    "no parentheses",
    "no English",
    "no punctuation at the end",
]


def write_glosses(body: dict) -> str:
    """Pseudo-translate each item and write its glosses: term（gloss）, but not ``OMITTED``."""
    items = batch_items(_last_user_content(body)) or []
    answer = []
    for item in items:
        output = PSEUDO_PREFIX + item["input"]
        for entry in item.get(GLOSS_FIELD, []):
            if (entry["term"], entry["gloss_zh"]) == OMITTED and ABSTRACT not in plain(item["input"]):
                continue
            gloss = entry["gloss_zh"].replace("行", "\ufa08")
            output = re.sub(
                re.escape(entry["term"]),
                lambda m, gloss=gloss: m.group(0) + "（" + gloss + "）",
                output,
                count=1,
                flags=re.IGNORECASE,
            )
        answer.append({"id": item["id"], "output": output})
    return json.dumps(answer, ensure_ascii=False)


def plain(text: str) -> str:
    text = re.sub(r"</?style[^>]*>", " ", text)
    return re.sub(r"\s+", " ", text).replace("- ", "-").strip()


@pytest.fixture(scope="module")
def maliang(tmp_path_factory):
    with FakeModel() as model:
        model.respond("term_list", lambda body: json.dumps({"terms": TERMS}, ensure_ascii=False))
        model.respond("translate_batch", write_glosses)
        service = launch_service(model, tmp_path_factory.mktemp("short-gloss"))
        try:
            task_id = service.submit(MALIANG_P1_P3)
            final = service.wait(task_id)
            report = service.report(task_id)
        finally:
            stop_service(service)
    assert final["state"] == "succeeded", final["error"]
    return {"model": model, "report": report}


def test_an_occurrence_inside_source_parentheses_is_not_a_first_mention(maliang):
    found = gloss_requests(maliang["model"].requests_of("translate_batch"))
    markers = [ABSTRACT, INTRO_2, PAGE_3_MLLM]

    # "(MLLMs)" on page 1 is skipped; the body gloss goes to page 3.
    assert sorted(where(found[MLLM_GLOSS], markers)) == sorted([ABSTRACT, PAGE_3_MLLM])
    # "(PEG)" is the only form of PEG, in the abstract and on page 3.
    assert "持久可执行生成状态" not in found

    pages = {item["concept"]: item for item in maliang["report"]["first_mentions"]}
    assert (pages["MLLMs"]["abstract_page"], pages["MLLMs"]["body_page"]) == (1, 2)
    assert (pages["PEG"]["abstract_page"], pages["PEG"]["body_page"]) == (None, None)


def test_the_task_report_records_whether_each_planned_gloss_was_written(maliang):
    report = maliang["report"]
    pages = {item["concept"]: item for item in report["first_mentions"]}

    # Written with U+FA08 for 行: found after NFKC normalisation.
    assert pages["MLLMs"]["abstract_written"] is True
    assert pages["MLLMs"]["body_written"] is True
    assert pages["programmable"]["abstract_written"] is True
    assert pages["programmable"]["body_written"] is False
    # No gloss planned: nothing to record.
    assert pages["PEG"]["abstract_written"] is None
    assert pages["PEG"]["body_written"] is None


def test_a_gloss_missing_from_the_translation_is_listed_in_the_task_report(maliang):
    glosses = maliang["report"]["first_mention_glosses"]
    planned = sum(
        (item["abstract_page"] is not None) + (item["body_page"] is not None)
        for item in maliang["report"]["first_mentions"]
    )
    assert glosses["planned"] == planned
    assert glosses["written"] == planned - 1

    [missing] = glosses["not_written"]
    assert {k: missing[k] for k in ("concept", "term", "gloss_zh", "range", "page")} == {
        "concept": "programmable",
        "term": "programmable",
        "gloss_zh": "以程序表达生成过程",
        "range": "body",
        "page": 1,
    }
    # The final translation of that paragraph, to see why.
    assert missing["translation"].startswith(PSEUDO_PREFIX)
    assert INTRO_2 in plain(missing["translation"])


def test_a_missing_gloss_is_not_translated_again(maliang):
    model = maliang["model"]
    sent = [
        plain(item["input"])
        for request in model.requests_of("translate_batch")
        for item in batch_items(request.prompt) or []
    ]
    assert len([text for text in sent if INTRO_2 in text]) == 1
    assert not [r for r in model.requests_of("single_paragraph") if INTRO_2 in plain(r.prompt)]


def test_translation_requests_carry_the_gloss_field_and_the_placement_rules(maliang):
    batches = maliang["model"].requests_of("translate_batch")
    with_glosses = [r for r in batches if any(GLOSS_FIELD in i for i in batch_items(r.prompt))]
    assert with_glosses

    for request in batches:
        instructions = squeeze(request.prompt.split("## Here is the input:", 1)[0])
        for rule in GENERAL_RULES:
            assert squeeze(rule) in instructions
        has_glosses = request in with_glosses
        for rule in GLOSS_RULES:
            assert (squeeze(rule) in instructions) == has_glosses


def test_review_requests_carry_the_same_rules(maliang):
    reviews = maliang["model"].requests_of("review")
    with_glosses = [r for r in reviews if GLOSS_FIELD in r.prompt.split("## Here is the input:")[-1]]
    assert with_glosses
    for request in with_glosses:
        instructions = squeeze(request.prompt.split("## Here is the input:", 1)[0])
        for rule in GLOSS_RULES + GENERAL_RULES:
            assert squeeze(rule) in instructions


def test_the_term_list_asks_for_short_noun_phrase_glosses(maliang):
    [request] = maliang["model"].requests_of("term_list")
    instructions = squeeze(request.body["messages"][0]["content"])
    for rule in TERM_LIST_GLOSS_RULES:
        assert squeeze(rule) in instructions
    assert squeeze("one short Chinese sentence") not in instructions


def squeeze(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


# One paragraph that names PEG inside parentheses first and outside them
# after. The acceptance papers have no such paragraph, so the test writes
# one on a generated page.
SAME_PARAGRAPH = (
    "We define the Persistent Executable Generation (PEG) state for visual programs. "
    "The PEG state keeps every program, every rendered result and the task context, "
    "so that later revisions can start from a known and inspectable point of the process."
)


def test_in_one_paragraph_the_gloss_asks_for_the_occurrence_outside_parentheses(
    tmp_path_factory,
):
    pdf = tmp_path_factory.mktemp("same-paragraph") / "same-paragraph.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 100, 520, 400), SAME_PARAGRAPH, fontsize=11)
        doc.save(pdf)
    terms = [key_concept(["PEG"], "持久可执行生成状态")]
    with FakeModel() as model:
        model.respond("term_list", lambda body: json.dumps({"terms": terms}, ensure_ascii=False))
        service = launch_service(model, tmp_path_factory.mktemp("same-paragraph-svc"))
        try:
            task_id = service.submit(pdf)
            final = service.wait(task_id)
        finally:
            stop_service(service)
    assert final["state"] == "succeeded", final["error"]

    batches = model.requests_of("translate_batch")
    [(term, text)] = gloss_requests(batches)["持久可执行生成状态"]
    assert term == "PEG"
    assert "(PEG)" in plain(text) and "The PEG state" in plain(text)
    [request] = [r for r in batches if any(GLOSS_FIELD in i for i in batch_items(r.prompt))]
    instructions = squeeze(request.prompt.split("## Here is the input:", 1)[0])
    assert squeeze(OUTSIDE_PARENTHESES) in instructions
