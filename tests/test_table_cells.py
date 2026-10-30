"""Table cells the model returns unchanged keep their source glyphs (closure ticket 06, T1, T2).

A table cell that holds a model or system name (``Claude Code``, ``Doubao``)
is one short paragraph, and the model answers it with the name as it is. The
engine is meant to keep the source glyphs of a paragraph whose translation
equals its input, but it compared the answer with the input object instead
of the input text, so the comparison never held: the name was set again in
the substitute font (Noto Serif for SFRM0700), and a wider substitute font
made some names smaller than the rest of their column.
"""

import json

import pymupdf
import pytest

from tests.conftest import FIXTURES
from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import batch_items

EXCERPT = FIXTURES / "autodesign-p11.pdf"
# Table 1 of AutoDesign page 11: its first column below the header row, the
# group labels ("Design Agent", "Coding Agent", ...) and the system names
# (PDF points from the top).
FIRST_COLUMN = pymupdf.Rect(60, 118, 125, 282)
# The system names of that column; the model returns them as they are. GLM
# and Kimi are shorter than the engine's shortest translated text and are
# never sent.
NAMES = {
    "AutoDesign",
    "Claude Design",
    "OpenDesign",
    "Codex",
    "Claude Code",
    "Doubao",
    "DeepSeek",
    "PosterGen",
    "Any2Poster",
    "Paper2Poster",
}
SIZE_TOLERANCE = 0.1


def names_unchanged(body: dict, respace=lambda name: name) -> str:
    """A batch answer that returns the system names unchanged (``respace``
    may change their whitespace) and pseudo-translates everything else."""
    answer = []
    for item in batch_items(body["messages"][-1]["content"]) or []:
        text = item["input"]
        output = respace(text) if text.strip() in NAMES else PSEUDO_PREFIX + text
        answer.append({"id": item["id"], "output": output})
    return json.dumps(answer, ensure_ascii=False)


def name_cells(pdf: bytes) -> list[tuple[str, set[str], list[float]]]:
    """The system-name lines of the first column, top first: the line's
    words joined by one space, its fonts and its span sizes."""
    cells = []
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        page = doc[0].get_text("dict", clip=FIRST_COLUMN)
    for block in page["blocks"]:
        for line in block.get("lines", []):
            spans = [span for span in line["spans"] if span["text"].strip()]
            text = " ".join(" ".join(span["text"].split()) for span in spans)
            if text in NAMES:
                cells.append(
                    (line["bbox"][1], text, {span["font"] for span in spans}, [span["size"] for span in spans])
                )
    return [cell[1:] for cell in sorted(cells)]


@pytest.mark.parametrize(
    "respace",
    [
        pytest.param(lambda name: name, id="unchanged"),
        # Only the whitespace differs: a leading space, doubled inner spaces.
        pytest.param(lambda name: " " + name.replace(" ", "  "), id="whitespace-only"),
    ],
)
def test_names_returned_unchanged_keep_their_source_font_and_size(fake_model, start_service, respace):
    fake_model.respond("translate_batch", lambda body: names_unchanged(body, respace))
    service = start_service(fake_model)
    task_id = service.submit(EXCERPT)
    final = service.wait(task_id)
    assert final["state"] == "succeeded", final["error"]

    source = name_cells(EXCERPT.read_bytes())
    mono = name_cells(service.download(task_id, "mono"))

    # Every name of the column, in the source order.
    assert [text for text, *_ in mono] == [text for text, *_ in source], mono
    for (text, source_fonts, source_sizes), (_, fonts, sizes) in zip(source, mono, strict=True):
        assert fonts == source_fonts, (text, fonts, source_fonts)
        assert all(abs(size - source_sizes[0]) <= SIZE_TOLERANCE for size in sizes), (text, sizes, source_sizes)
