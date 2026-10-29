"""Reference entries and long URLs in the translation (closure ticket 04, R2, R3).

R2: one layout box can cover several reference entries, and the engine made
one paragraph of each box, so the translation ran the entries together
(AutoDesign page 17 had 8 entries in one paragraph). The source sets each
entry with a hanging indent: its first line starts at the entry x, the other
lines about 1 em to the right.

R3: a run of characters with no break point that was longer than the line
was set one character per line: the line break check moved the run to a new
line, set its first character, and the next character broke the line again
(the NeurIPS URL on AutoRef page 12 in the M1 acceptance run). Since ticket
02 the source text has a space where that URL breaks its source line, so the
real papers no longer show it; the last test sets such a run directly.
"""

import re
from collections import defaultdict
from types import SimpleNamespace

import pymupdf
import pytest
from babeldoc.format.pdf.document_il import Box
from babeldoc.format.pdf.document_il import Document
from babeldoc.format.pdf.document_il import Page
from babeldoc.format.pdf.document_il import PdfCharacter
from babeldoc.format.pdf.document_il import PdfLine
from babeldoc.format.pdf.document_il import PdfParagraph
from babeldoc.format.pdf.document_il import PdfParagraphComposition
from babeldoc.format.pdf.document_il import PdfStyle
from babeldoc.format.pdf.document_il import VisualBbox
from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
from babeldoc.format.pdf.document_il.midend.typesetting import MIN_SCALE

from tests.conftest import FIXTURES
from tests.test_line_spacing import bare_typesetting
from tests.test_line_spacing import char_units

LATIN = re.compile(r"[A-Za-z0-9]")
# The page of each excerpt to check (0-based) and its reference list, below
# the heading or running header and above the page number (PDF points from the
# top of the page). AutoRef page 10 is in its excerpt for the REFERENCES
# heading, which starts the reference list.
REFERENCE_AREA = {
    "autodesign-p17.pdf": (0, pymupdf.Rect(0, 90, 612, 740)),
    "autoref-p10-p12.pdf": (1, pymupdf.Rect(0, 75, 612, 740)),
}


def text_rows(data: bytes, page: int, area: pymupdf.Rect) -> list[tuple[float, str]]:
    """``(left x, characters)`` of each text row on ``page`` inside ``area``.

    A row is the characters on one baseline; a vertical run of single letters
    is one row per letter.
    """
    rows: dict[int, list[tuple[float, str]]] = defaultdict(list)
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        raw = doc[page].get_text("rawdict", clip=area)
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    if char["c"].strip():
                        rows[round(char["origin"][1])].append((char["bbox"][0], char["c"]))
    return [
        (min(x for x, _ in chars), "".join(c for _, c in sorted(chars)))
        for _, chars in sorted(rows.items())
    ]


def entry_starts(rows: list[tuple[float, str]], entry_x: float) -> int:
    """Rows that start at the entry x: the first line of an entry (the other
    lines of an entry are indented about 1 em)."""
    return sum(1 for x, _ in rows if abs(x - entry_x) < 4)


def single_letter_rows(rows: list[tuple[float, str]]) -> list[tuple[float, str]]:
    return [(x, text) for x, text in rows if len(text) == 1 and LATIN.match(text)]


def translate(fake_model, start_service, name: str) -> tuple[bytes, bytes]:
    """The excerpt and its single-language translation (pseudo-translated)."""
    service = start_service(fake_model)
    pdf = FIXTURES / name
    task_id = service.submit(pdf)
    final = service.wait(task_id)
    assert final["state"] == "succeeded", final["error"]
    return pdf.read_bytes(), service.download(task_id, "mono")


def check_entries(source: bytes, mono: bytes, name: str) -> None:
    page, area = REFERENCE_AREA[name]
    source_rows = text_rows(source, page, area)
    mono_rows = text_rows(mono, page, area)
    entry_x = min(x for x, _ in source_rows)
    # Each entry is a paragraph of its own: its first line starts at the entry
    # x, as in the source, and its other lines keep the hanging indent. No
    # line holds a single letter.
    assert (entry_starts(mono_rows, entry_x), single_letter_rows(mono_rows)) == (
        entry_starts(source_rows, entry_x),
        [],
    ), [text[:30] for x, text in mono_rows if abs(x - entry_x) < 4]


def test_each_entry_of_autodesign_page_17_starts_a_line(fake_model, start_service):
    name = "autodesign-p17.pdf"
    source, mono = translate(fake_model, start_service, name)
    check_entries(source, mono, name)


def test_the_entries_and_urls_of_autoref_page_12(fake_model, start_service):
    name = "autoref-p10-p12.pdf"
    source, mono = translate(fake_model, start_service, name)
    check_entries(source, mono, name)


def set_paragraph(units, width: float) -> tuple[float, list[str]]:
    """The scale and the lines of a paragraph set in a box ``width`` wide and
    1000 pt high (the typesetter's scale search, as for a real paragraph)."""
    paragraph = SimpleNamespace(
        box=Box(x=0, y=0, x2=width, y2=1000),
        debug_id="p",
        first_line_indent=False,
        hanging_indent=None,
        scale=None,
        pdf_paragraph_composition=[],
    )
    page = SimpleNamespace(pdf_curve=[], pdf_form=[])
    scale, laid_out = bare_typesetting()._find_optimal_scale_and_layout(
        paragraph, page, units, apply_layout=True
    )
    lines = {}
    for unit in laid_out:
        lines.setdefault(round(unit.box.y, 3), []).append(unit.try_get_unicode())
    return scale, ["".join(line) for line in lines.values()]


def test_a_run_longer_than_the_line_at_the_scale_floor_breaks_at_the_right_edge():
    # A URL has no break point: "/", "." and ":" do not break an English line
    # in the engine, only spaces do. The URL is 80 letters of 5 pt: even at
    # the scale floor (2.5 pt a letter, 200 pt) it is longer than the 101 pt
    # line, so it breaks there at the right edge. "中文 " ends at 11.5.
    url = "https://proceedings.neurips.cc/paper/2025/file/40168e00bf87869c5d153e934d8a3602"
    widths = {"中": 10, "文": 10, " ": 3} | dict.fromkeys(url, 5)

    scale, lines = set_paragraph(char_units("中文 " + url, widths), 101)

    assert scale == pytest.approx(MIN_SCALE)
    # The URL starts after "中文 " and each line is filled to the right edge;
    # it was one letter per line.
    assert lines == ["中文 " + url[:35], url[35:75], url[75:]]


def test_a_word_longer_than_a_narrow_box_is_shrunk_whole():
    # A figure label left in English ("Idea2Img" on AutoRef page 8) in a box
    # narrower than the word: the paragraph is set smaller and the word stays
    # whole, as before ticket 04 (it was set at 1.0 as "Idea2I" / "mg").
    widths = dict.fromkeys("Idea2Img", 5)

    scale, lines = set_paragraph(char_units("Idea2Img", widths), 31)

    assert scale < 1
    assert lines == ["Idea2Img"]


def source_line(rows: list[tuple[str, float, float]]) -> PdfParagraphComposition:
    """One line of the paragraph finder holding ``(text, x, y)`` rows of 9 pt
    characters 5 pt wide."""
    chars = []
    for text, x, y in rows:
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
    line = PdfLine(pdf_character=chars)
    ParagraphFinder.update_line_data(None, line)
    return PdfParagraphComposition(pdf_line=line)


def test_entries_that_the_line_finder_kept_whole_are_cut_apart():
    # In the whole AutoDesign paper the line finder makes one "line" of each
    # entry of page 17 (the 17 pt gap between entries separates them, the
    # 11 pt line pitch inside an entry does not), so no line starts indented:
    # the entries must be told apart by the rows inside the lines.
    lines = [
        source_line([("Agrawal", 71, 700), ("learning", 81, 689)]),
        source_line([("Bradley", 71, 670), ("Biometrika", 81, 659), ("doi", 81, 648)]),
        source_line([("Chameleon", 71, 630)]),
    ]
    paragraph = PdfParagraph(pdf_paragraph_composition=lines, layout_id=1, debug_id="p")
    page = Page(page_number=16, pdf_paragraph=[paragraph])
    finder = object.__new__(ParagraphFinder)
    finder.translation_config = SimpleNamespace(reference_section=lambda found: range(len(found)))

    finder._split_reference_entries(Document(page=[page]))

    entries = [
        (
            "".join(c.char_unicode for p in e.pdf_paragraph_composition for c in p.pdf_line.pdf_character),
            round(e.hanging_indent or 0),
        )
        for e in page.pdf_paragraph
    ]
    assert entries == [
        ("Agrawallearning", 10),
        ("BradleyBiometrikadoi", 10),
        ("Chameleon", 10),
    ]
