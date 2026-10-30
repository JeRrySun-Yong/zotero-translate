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
