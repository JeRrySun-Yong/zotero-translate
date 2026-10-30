"""Figure labels keep every character of their translation (closure ticket 15).

The engine writes a translated figure label into the figure's form XObject,
and a PDF viewer clips everything outside the form's bounding box. AutoRef
page 8: the legend label "w/ Qwen3-VL-32B" (answer "搭配 Qwen3-VL-32B") did not
fit its box, wrapped to a second line below the plot and showed only "搭配";
page 26: the plot title "Qwen-Image-Edit-2511 MultiBanana (4 Ref.)" (answer
"…MultiBanana（4张参考图）") ran past the plot's right edge and showed only
"…MultiBanana（4". The glyphs were in the content stream, so the checks on the
answers did not see the loss; the task report now lists such text
(``lost_text``).
"""

import json
import unicodedata

import pymupdf
import pytest

from tests.conftest import FIXTURES
from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import batch_items
from zotero_translate.lost_text import lost_text

# The model's answers in the C-10 run (model cache) for the labels at stake;
# every other paragraph is pseudo-translated.
ANSWERS = {
    "w/ Qwen3-VL-32B": "搭配 Qwen3-VL-32B",
    "w/ GPT-5.5": "搭配 GPT-5.5",
    "Vanilla": "原生",
    "Qwen-Image-Edit-2511 MultiBanana (4 Ref.)": "Qwen-Image-Edit-2511 MultiBanana（4张参考图）",
}
EXCERPTS = {
    "autoref-p8.pdf": ["搭配 Qwen3-VL-32B", "搭配 GPT-5.5"],
    "autoref-p26.pdf": ["Qwen-Image-Edit-2511 MultiBanana（4张参考图）"],
}


def label_answers(body: dict) -> str:
    answer = []
    for item in batch_items(body["messages"][-1]["content"]) or []:
        text = item["input"]
        answer.append({"id": item["id"], "output": ANSWERS.get(text.strip(), PSEUDO_PREFIX + text)})
    return json.dumps(answer, ensure_ascii=False)


def norm(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text) if not c.isspace())


@pytest.mark.parametrize("excerpt", sorted(EXCERPTS))
def test_every_character_of_a_figure_label_is_on_the_page(fake_model, start_service, tmp_path, excerpt):
    fake_model.respond("translate_batch", label_answers)
    service = start_service(fake_model)
    source = FIXTURES / excerpt
    task_id = service.submit(source)
    final = service.wait(task_id)
    assert final["state"] == "succeeded", final["error"]

    mono = tmp_path / "mono.pdf"
    mono.write_bytes(service.download(task_id, "mono"))
    with pymupdf.open(mono) as doc:
        visible = norm(doc[0].get_text())
    for label in EXCERPTS[excerpt]:
        assert norm(label) in visible, (label, visible)
    # No answer text anywhere on the page is drawn outside what the page shows,
    # and the task report says so with the same judgment.
    assert lost_text(source, mono) == []
    assert service.report(task_id)["lost_text"] == []


# The left plot of AutoRef page 8 is a form whose box ends at y 444.03 (page
# coordinates from the top; the engine's IL pdf_xobject.box). The legend
# labels sit on that edge: the source baseline is at 442.6.
P8_LEGEND = pymupdf.Rect(150, 438, 213, 452)
P8_FORM_BOTTOM = 444.03
# A glyph box may reach this far past the form's edge (pt; rounding).
EDGE_TOLERANCE = 0.05


def test_legend_glyphs_stay_whole_inside_the_plot(fake_model, start_service, tmp_path):
    """Review round 1: with only the centre of each glyph inside the form,
    the legend's baseline came 0.34 pt below the form's edge and about 30 %
    of each glyph box (descender included) was cut off."""
    fake_model.respond("translate_batch", label_answers)
    service = start_service(fake_model)
    task_id = service.submit(FIXTURES / "autoref-p8.pdf")
    final = service.wait(task_id)
    assert final["state"] == "succeeded", final["error"]

    mono = tmp_path / "mono.pdf"
    mono.write_bytes(service.download(task_id, "mono"))
    with pymupdf.open(mono) as doc:
        glyphs = [
            (chr(code), bbox)
            for span in doc[0].get_texttrace()
            for code, _glyph, _origin, bbox in span["chars"]
            if code > 32 and P8_LEGEND.contains(pymupdf.Point((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2))
        ]
    text = "".join(c for c, _ in glyphs)
    assert norm("搭配 Qwen3-VL-32B") in text and norm("搭配 GPT-5.5") in text, text
    below = [(c, round(b[3], 2)) for c, b in glyphs if b[3] > P8_FORM_BOTTOM + EDGE_TOLERANCE]
    assert below == [], below


# AutoRef page 1, figure 1 (b): the y-axis labels "3 Refs.", "4 Refs.",
# "5 Refs." are right-aligned at x 265.1, with 23.9 pt free to their left up
# to the form's edge; the C-10 answers.
AXIS_ANSWERS = {
    "3 Refs.": "3张参考图",
    "4 Refs.": "4张参考图",
    "5 Refs.": "5 张参考图",
    "(unseen)": "（未见）",
    "(searched)": "（已搜索）",
}


def axis_answers(body: dict) -> str:
    answer = []
    for item in batch_items(body["messages"][-1]["content"]) or []:
        text = item["input"].strip()
        answer.append({"id": item["id"], "output": AXIS_ANSWERS.get(text, ANSWERS.get(text, PSEUDO_PREFIX + text))})
    return json.dumps(answer, ensure_ascii=False)


def test_a_right_aligned_axis_label_widens_to_the_left(fake_model, start_service, tmp_path):
    """Review round 1: "3 Refs." kept its source text although 23.9 pt were
    free on its left; widening knew only "centred: both sides" and "left
    aligned: to the right"."""
    fake_model.respond("translate_batch", axis_answers)
    service = start_service(fake_model)
    task_id = service.submit(FIXTURES / "autoref-p1.pdf")
    final = service.wait(task_id)
    assert final["state"] == "succeeded", final["error"]

    kept = [k["source"] for k in service.report(task_id)["kept_source_paragraphs"]]
    assert not any("Refs" in source for source in kept), kept
    mono = tmp_path / "mono.pdf"
    mono.write_bytes(service.download(task_id, "mono"))
    with pymupdf.open(mono) as doc:
        lines = [
            (norm("".join(s["text"] for s in line["spans"])), line["bbox"])
            for block in doc[0].get_text("dict")["blocks"]
            for line in block.get("lines", [])
        ]
    labels = {text: bbox for text, bbox in lines if text in {"3张参考图", "4张参考图", "5张参考图"}}
    assert set(labels) == {"3张参考图", "4张参考图", "5张参考图"}, [t for t, _ in lines if "参考" in t]
    # Right-aligned like the source labels, so they stay clear of the bars
    # that start right of x 265.1.
    for text, bbox in labels.items():
        assert bbox[2] <= 265.1 + 1.0, (text, bbox)
