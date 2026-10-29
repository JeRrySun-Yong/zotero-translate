"""Word spaces the engine infers between glyphs (closure ticket 02, W1–W4).

The source PDFs have no space glyphs between these words: the engine adds a
space where the gap between two characters is wide enough. The rule took the
second smallest distinct gap of a line as the threshold, so word gaps that
differ only by rounding (1.84988 and 1.8499) or a line with one wider
sentence gap lost their spaces: the authors on AutoDesign page 1 came out as
``XiaofuChen`` (the translation reuses the source glyphs of an unchanged
author line), and ``Appendix H.1`` on AutoRef page 7 reached the model as
``AppendixH.1``.
"""

import json
import re

import pymupdf

from tests.conftest import FIXTURES
from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import batch_items
from tests.test_paper_task import pdf_text

STYLE_TAG = re.compile(r"</?style[^>]*>")
# "Appendix H.1", the "H.1" possibly in a style tag of its own (a link).
APPENDIX_H1 = re.compile(r"Appendix(\s*(?:</?style[^>]*>\s*)*)H\.1")
AUTHORS = ["Xiaofu Chen", "Jiacheng Liu", "Jiacheng Cui", "Zhiqiang Shen", "Xiaotong Li"]


def page_text(data: bytes, page: int) -> str:
    """One page of a PDF as PyMuPDF extracts it, line breaks as spaces."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return re.sub(r"\s+", " ", doc[page - 1].get_text())


def like_the_model(body: dict) -> str:
    """A draft like the real model writes: the author line unchanged (names are
    not translated), ``Appendix H.1`` as "附录 H.1", everything else pseudo-translated."""
    answer = []
    for item in batch_items(body["messages"][-1]["content"]) or []:
        text = item["input"]
        if "Xiaofu" in STYLE_TAG.sub("", text):
            output = text
        else:
            output = PSEUDO_PREFIX + APPENDIX_H1.sub(r"附录 \1H.1", text)
        answer.append({"id": item["id"], "output": output})
    return json.dumps(answer, ensure_ascii=False)


def batch_inputs(fake_model) -> list[str]:
    return [
        STYLE_TAG.sub("", item["input"])
        for r in fake_model.requests_of("translate_batch")
        for item in batch_items(r.prompt)
    ]


def test_the_author_names_on_autodesign_page_1_keep_their_spaces(fake_model, start_service):
    fake_model.respond("translate_batch", like_the_model)
    service = start_service(fake_model)

    task_id = service.submit(FIXTURES / "autodesign-p1.pdf")
    final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    # The model sees the names with their spaces.
    authors = [text for text in batch_inputs(fake_model) if "Xiaofu" in text]
    assert authors, "no batch carried the author line"
    for name in AUTHORS:
        assert any(name in text for text in authors), name
    # The unchanged author line reuses the source glyphs, spaces included.
    mono = page_text(service.download(task_id, "mono"), 1)
    for name in AUTHORS:
        surname = name.split()[1]
        assert name in mono, mono[mono.find(surname) - 40 : mono.find(surname) + 40]
        assert name.replace(" ", "") not in mono, name


def test_the_appendix_h1_paragraph_on_autoref_page_7_is_translated(fake_model, start_service):
    fake_model.respond("translate_batch", like_the_model)
    service = start_service(fake_model)

    task_id = service.submit(FIXTURES / "autoref-p7-p14.pdf")
    final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    # The model sees "Appendix H.1", not the glued "AppendixH.1".
    inputs = batch_inputs(fake_model)
    assert any("Appendix H.1" in text for text in inputs)
    assert not any("AppendixH.1" in text for text in inputs)
    # The draft "附录 H.1" passes the checks: the paragraph is translated.
    assert final["kept_source_paragraphs"] == 0
    assert service.report(task_id)["review"]["drafts_used"] == []
    assert "附录H.1" in pdf_text(service.download(task_id, "mono"))
