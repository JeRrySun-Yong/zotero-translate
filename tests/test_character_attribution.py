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
from types import SimpleNamespace

import pymupdf
from babeldoc.format.pdf.document_il import Box
from babeldoc.format.pdf.document_il import Page
from babeldoc.format.pdf.document_il import PageLayout
from babeldoc.format.pdf.document_il import PdfCharacter
from babeldoc.format.pdf.document_il import PdfStyle
from babeldoc.format.pdf.document_il import VisualBbox
from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
from babeldoc.format.pdf.document_il.utils.layout_helper import build_layout_index

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


def line_chars(text: str, x: float, y: float) -> list[PdfCharacter]:
    """The characters of one line of 9 pt text, 5 pt a letter, from ``x``."""
    chars = []
    for k, c in enumerate(text):
        box = Box(x=x + 5 * k, y=y, x2=x + 5 * k + 5, y2=y + 9)
        chars.append(
            PdfCharacter(
                char_unicode=c,
                box=box,
                visual_bbox=VisualBbox(box=box),
                pdf_style=PdfStyle(font_id="F", font_size=9),
            )
        )
    return chars


def boxes_of(chars: list[PdfCharacter], layouts: list[tuple[str, Box]]) -> str:
    """The class of the box each character of one line goes to, as one letter
    each: T for the text box, L for the line's own box."""
    x, y = chars[0].visual_bbox.box.x, min(c.visual_bbox.box.y for c in chars)
    x2, y2 = chars[-1].visual_bbox.box.x2, max(c.visual_bbox.box.y2 for c in chars)
    page = Page(
        page_number=0,
        pdf_character=chars,
        page_layout=[
            PageLayout(id=i + 1, box=box, conf=1, class_name=name)
            for i, (name, box) in enumerate(layouts)
        ]
        + [
            PageLayout(
                id=len(layouts) + 1,
                box=Box(x=x, y=y, x2=x2, y2=y2),
                conf=1,
                class_name="fallback_line",
            )
        ],
    )
    finder = object.__new__(ParagraphFinder)
    finder.translation_config = SimpleNamespace()
    layouts_found = finder._line_layouts(page, *build_layout_index(page))
    return "".join("L" if layout.name == "fallback_line" else "T" for layout in layouts_found)


def test_a_line_mostly_left_of_its_text_box_joins_the_box():
    # AutoDesign page 21: the second line of table 5's caption, "named in the
    # final column.", starts 92 pt left of the caption box; only "umn." lies
    # in it, and stayed untranslated beside the line's translation. The box
    # holds the line from top to bottom: the line is part of its text.
    text = "named in the final column."
    caption = ("plain text", Box(x=163, y=160, x2=536, y2=185))

    assert boxes_of(line_chars(text, 71, 165), [caption]) == "T" * len(text)


def test_a_line_that_a_box_edge_clips_keeps_its_own_box():
    # AutoDesign page 22: the bottom edge of a text box above reaches 0.4 pt
    # into the line "- Build visual evidence units deliberately.", into its
    # final "." only, which was set alone. The box does not hold the line:
    # the whole line stays one paragraph of its own.
    text = "- Build visual evidence units deliberately."
    chars = line_chars(text, 79, 203)
    chars[-1].visual_bbox.box.y2 = 212.4
    above = ("plain text", Box(x=78, y=212, x2=533, y2=251))

    assert boxes_of(chars, [above]) == "L" * len(text)
