"""Algorithm detection rules on synthetic PDFs (M2 ticket 01, review round 1).

These call the detector directly on the characters of small generated PDFs,
for rules the acceptance papers do not exercise: algorithms without ``←``,
a ``Procedure`` title, and an algorithm that continues on the next page
(none of the three acceptance papers has one).
"""

import pymupdf

from zotero_translate.algorithms import AlgorithmDetector
from zotero_translate.algorithms import Glyph
from zotero_translate.algorithms import keep_flags


def glyphs_of(page: pymupdf.Page) -> list[Glyph]:
    """The page's characters as the detector sees them (y grows upwards)."""
    height = page.rect.height
    glyphs = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    x0, y0, x1, y1 = char["bbox"]
                    glyphs.append(
                        Glyph(char["c"], x0, height - y1, x1, height - y0, span["font"], span["size"])
                    )
    return glyphs


def detect(pages: list[list[tuple[int | None, str]]]) -> AlgorithmDetector:
    """Write each page's rows (line number or None, text) into a PDF and detect."""
    doc = pymupdf.open()
    for rows in pages:
        page = doc.new_page()
        y = 100
        for number, text in rows:
            if number is not None:
                page.insert_text((100, y), f"{number}:", fontname="helv", fontsize=10)
            page.insert_text((118, y), text, fontname="helv", fontsize=10)
            y += 12
    detector = AlgorithmDetector()
    for index, page in enumerate(doc):
        detector(index + 1, glyphs_of(page))
    return detector


def test_keywords_without_an_arrow_make_an_algorithm():
    detector = detect(
        [[(1, "x = 0"), (2, "for i = 1 to n do"), (3, "x = x + i"), (4, "end for"), (5, "return x")]]
    )
    assert [(u.report()["pages"], u.report()["numbered_lines"]) for u in detector.units] == [([1], 5)]


def test_a_require_row_alone_is_a_keyword():
    detector = detect([[(None, "Require: a list L"), (1, "sort L"), (2, "print L"), (3, "stop")]])
    assert len(detector.units) == 1


def test_numbered_lines_without_a_keyword_are_not_an_algorithm():
    detector = detect([[(1, "Apples are red."), (2, "Bananas are yellow."), (3, "Grapes are purple.")]])
    assert detector.units == []


def test_a_procedure_row_is_the_title():
    detector = detect(
        [[(None, "Procedure Draw(x)"), (1, "y = 0"), (2, "for i = 1 to x do"), (3, "end for")]]
    )
    (unit,) = detector.units
    assert unit.title == "Procedure Draw(x)"
    assert unit.fragments[0].lines[0].text == "Procedure Draw(x)"


def test_numbers_that_continue_on_the_next_page_are_one_algorithm():
    detector = detect(
        [
            [(1, "x = 0"), (2, "for i = 1 to n do"), (3, "x = x + i")],
            [(4, "end for"), (5, "return x"), (6, "y = x")],
        ]
    )
    (unit,) = detector.units
    report = unit.report()
    assert report["pages"] == [1, 2]
    assert (report["numbered_lines"], report["first_line"], report["last_line"]) == (6, 1, 6)


def test_a_new_title_on_the_next_page_starts_another_algorithm():
    detector = detect(
        [
            [(1, "x = 0"), (2, "for i = 1 to n do"), (3, "x = x + i")],
            [(None, "Algorithm 2 Next"), (4, "end for"), (5, "return x"), (6, "y = x")],
        ]
    )
    assert [u.report()["pages"] for u in detector.units] == [[1], [2]]
    assert detector.units[1].title == "Algorithm 2 Next"


def row(*runs: tuple[str, str, bool, float]) -> list[Glyph]:
    """Glyphs of one row from (text, font, bold, size) runs; a space is a word gap."""
    glyphs, x = [], 100.0
    for text, font, bold, size in runs:
        for ch in text:
            if ch == " ":
                x += 3
                continue
            glyphs.append(Glyph(ch, x, 100, x + 0.5 * size, 100 + size, font, size, bold))
            x += 0.5 * size
    return glyphs


def kept(glyphs: list[Glyph]) -> str:
    return "".join(g.text if k else "_" for g, k in zip(glyphs, keep_flags(glyphs)))


TEXT, BOLD, MATH = "NimbusRomNo9L-Regu", "NimbusRomNo9L-Medi", "CMMI10"


def test_keywords_formulas_and_calls_keep_their_source_text():
    """Ticket 02: what a translated row keeps (D5: call arguments are identifiers)."""
    loop = row(("for", BOLD, True, 10), (" each position ", TEXT, False, 10), ("do", BOLD, True, 10))
    assert kept(loop) == "for" + "_" * 12 + "do"
    call = row(
        ("F", TEXT, False, 10), ("ILL", TEXT, False, 8), ("(", TEXT, False, 10),
        ("C,", MATH, False, 10), (" hair silhouette)", TEXT, False, 10),
    )
    assert kept(call) == "FILL(C,hairsilhouette)"
    to = row(
        ("for ", BOLD, True, 10), ("i=0 ", MATH, False, 10), ("to ", TEXT, False, 10),
        ("25 ", MATH, False, 10), ("do", BOLD, True, 10),
    )
    assert "_" not in kept(to)
    braces = row(("d∈{", MATH, False, 10), ("left", TEXT, False, 10), ("}", MATH, False, 10))
    assert "_" not in kept(braces)
    heading = row(("Asset generation and selection", BOLD, True, 10))
    assert set(kept(heading)) == {"_"}
    title = row(("Algorithm 2 ", BOLD, True, 10), ("Procedural generation", TEXT, False, 10))
    assert kept(title).startswith("Algorithm2_")
