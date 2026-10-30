"""Algorithms on two-column pages stay in their column (closure ticket 08, red team A3).

The rows of an algorithm, its title row included, stay in the column of its
line numbers: text of the other column at the same height is not part of
the algorithm, however narrow the column gap. Synthetic pages as in the red
team's probe (``evidence/redteam/synth3.py``: column gap 18 pt or 30 pt,
9 pt glyphs, six offsets of the right column, with and without a title and a
line that runs to the column's edge) and the M2 review's sweep
(``m2-01-detection/sweep_arrow.py``: a right-column row between the title
and line 1, twelve offsets).
"""

import pymupdf
import pytest

from tests.test_algorithm_rules import glyphs_of
from zotero_translate.algorithms import AlgorithmDetector
from zotero_translate.algorithms import Glyph


def text_glyphs(
    text: str, x: float, base: float, size: float = 9.0, font: str = "NimbusRomNo9L-Regu"
) -> list[Glyph]:
    """One glyph per character, 0.5 x size wide (``evidence/redteam/synth.py``)."""
    glyphs = []
    for ch in text:
        width = 0.5 * size
        glyphs.append(Glyph(ch, x, base - 0.2 * size, x + width, base + 0.8 * size, font, size))
        x += width
    return glyphs


LEFT, COLUMN = 54.0, 234.0  # the left column: x 54 to 288
ALGORITHM = ["x ← 0", "for i = 1 to n do", "x", "end for", "return x"]
# Line 3 when it runs to the left column's edge.
LONG = ("update the agent memory with the observation " * 10)[: int((COLUMN - 1 - 14) / 4.5)]


def two_columns(gap: float, title: bool, offset: int, long_line: bool) -> tuple[list[Glyph], float]:
    """An algorithm in the left column and 14 rows of prose in the right one; the right column's x."""
    right = LEFT + COLUMN + gap
    glyphs = text_glyphs("Algorithm 1 Sum", LEFT, 500) if title else []
    for k, text in enumerate(ALGORITHM):
        base = 488 - 11 * k
        glyphs += text_glyphs(f"{k + 1}:", LEFT, base)
        glyphs += text_glyphs(LONG if k == 2 and long_line else text, LEFT + 14, base)
    for k in range(14):
        glyphs += text_glyphs("Right column prose about agents and video.", right, 520 + offset - 11 * k)
    return glyphs, right


@pytest.mark.parametrize("long_line", [False, True], ids=["short", "long-line"])
@pytest.mark.parametrize("title", [True, False], ids=["title", "no-title"])
@pytest.mark.parametrize("gap", [18.0, 30.0])
def test_an_algorithm_takes_no_glyph_of_the_other_column(gap, title, long_line):
    absorbed = {}
    units = {}
    for offset in range(0, 11, 2):
        glyphs, right = two_columns(gap, title, offset, long_line)
        detector = AlgorithmDetector()
        detector(1, glyphs)
        (unit,) = detector.units
        fragment = unit.fragments[0]
        absorbed[offset] = sum(1 for i in fragment.glyphs if glyphs[i].x0 >= right - 0.1 and not glyphs[i].blank)
        units[offset] = (unit.title, unit.numbers, [line.text.strip() for line in fragment.lines])
    assert absorbed == dict.fromkeys(range(0, 11, 2), 0)
    # The algorithm itself is whole: its title, its 5 lines, the long line to its end.
    rows = (["Algorithm 1 Sum"] if title else []) + [
        LONG.strip() if k == 2 and long_line else text for k, text in enumerate(ALGORITHM)
    ]
    expected = ("Algorithm 1 Sum" if title else None, [1, 2, 3, 4, 5], rows)
    assert units == dict.fromkeys(range(0, 11, 2), expected)


@pytest.mark.parametrize("distance", [14, 18, 22])
def test_a_row_of_the_other_column_between_the_title_and_line_1_stays_out(distance):
    absorbed = []
    for offset in range(12):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((60, 200 - distance), "Algorithm 1 Sum", fontname="helv", fontsize=10)
        for k, text in enumerate(["x = 0", "for i = 1 to n do", "x = x + i", "end for", "return x"]):
            page.insert_text((60, 200 + 12 * k), f"{k + 1}:", fontname="helv", fontsize=10)
            page.insert_text((78, 200 + 12 * k), text, fontname="helv", fontsize=10)
        for k in range(16):
            page.insert_text(
                (320, 100 + offset + 12 * k), f"Right column prose line {k} here.", fontname="helv", fontsize=10
            )
        detector = AlgorithmDetector()
        detector(1, glyphs_of(page))
        (unit,) = detector.units
        assert (unit.title, unit.numbers) == ("Algorithm 1 Sum", [1, 2, 3, 4, 5])
        if any("Right" in line.text for line in unit.fragments[0].lines):
            absorbed.append(offset)
    assert absorbed == []
