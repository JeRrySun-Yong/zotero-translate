"""A line of source text goes to one paragraph (closure ticket 07, G2: the
residue in T1 captions, T3, O2, O3).

The paragraph finder gave each character the layout box it lies in. A
caption box narrower than its text (AutoRef page 30, figure 13: the box
starts 17 pt right of the line) left the characters outside it to the
box of their own text line, so "Figu" and "back" became paragraphs of their
own; two caption boxes, one inside the other (AutoRef page 17, table 5),
split each line between them. Fragments shorter than 5 characters are not
translated and stayed as source glyphs beside the translation.
"""

import re

import pymupdf

from tests.conftest import FIXTURES
from tests.fake_model import PSEUDO_PREFIX

EXCERPT = FIXTURES / "autoref-p17-p30.pdf"
# The caption of table 5 (AutoRef page 17) and of figure 13 (page 30): the
# page of the excerpt and the caption's two lines, in PDF points from the top.
CAPTIONS = {
    "table 5": (0, pymupdf.Rect(100, 262, 512, 287)),
    "figure 13": (1, pymupdf.Rect(100, 359, 512, 384)),
}


def text_in(data: bytes, page: int, area: pymupdf.Rect) -> str:
    """The characters on ``page`` whose centre lies in ``area``, in reading
    order, without whitespace."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        raw = doc[page].get_text("rawdict")
    chars = []
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    x0, y0, x1, y1 = char["bbox"]
                    if area.contains(pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)):
                        chars.append(char["c"])
    return re.sub(r"\s+", "", "".join(chars))


def test_each_caption_is_one_paragraph_with_no_source_residue(fake_model, start_service):
    service = start_service(fake_model)
    task_id = service.submit(EXCERPT)
    final = service.wait(task_id)
    assert final["state"] == "succeeded", final["error"]
    mono = service.download(task_id, "mono")
    source = EXCERPT.read_bytes()

    found = {
        name: text_in(mono, page, area) for name, (page, area) in CAPTIONS.items()
    }

    # The pseudo-translation is the prefix and the input. Each caption is
    # one paragraph: one prefix, then the whole caption as in the source,
    # with no fragment left in English ("Figu", "e", "back") and no other
    # paragraph ("Table" / "5: Model ..." / "ct revis").
    assert found == {
        name: PSEUDO_PREFIX + text_in(source, page, area)
        for name, (page, area) in CAPTIONS.items()
    }
