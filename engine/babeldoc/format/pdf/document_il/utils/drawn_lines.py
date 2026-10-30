"""Drawn lines that belong to a formula or to an identifier (closure ticket 14).

TeX draws some parts of a formula as lines, not glyphs: the bar of a radical
(the sign is a glyph, its bar a line), the bar of a fraction, and the
underscore of an identifier (``num\\_training\\_steps``: a 0.06 em kern and a
0.3 em rule on the baseline). Such a line has to move with the glyphs it
belongs to, and those glyphs must not be broken apart. The engine's body
text (``StylesAndFormulas.merge_formulas_with_drawn_lines``) and the
algorithm detector (``zotero_translate.algorithms``) both ask ``owners``
which glyphs of a text line a drawn line belongs to.

A drawn line (``line_of``) is a stroke or filled box at most ``LINE_HEIGHT``
pt high and wider than it is high. It belongs to glyphs of one text line when
both of its ends touch a glyph (the end is within ``TOUCH`` pt of the glyph's
box sideways, and the line within ``TOUCH`` pt of it in height; for a
formula's line, within ``TOUCH`` + ``END_REACH`` x the size of a formula
glyph) and

- it is a formula's line: a glyph at one of its ends or under or over it
  (the glyph's centre between the line's ends) is a formula glyph, and the
  glyphs under or over it spell no word of ``WORD_LETTERS`` letters or more
  outside formulas. The glyphs it belongs to are the glyphs under or over it
  and the formula glyphs at its ends (a radical's sign). A rule above or
  below a line of text (a table's rule, a running head's rule, a footnote
  rule, the three rules of an algorithm) has words under or over it, or no
  glyph at one of its ends, so it belongs to no formula. Or

- it is an underscore: no glyph is under or over it, it is at most
  ``UNDERSCORE_WIDTH`` x the glyphs' size long, it lies in the bottom
  ``UNDERSCORE_HEIGHT`` of the glyphs at its ends, and these are a letter or
  digit that ends within ``TOUCH`` pt left of it and one that starts within
  ``TOUCH`` pt right of it. It belongs to those two glyphs; the caller takes
  their whole words (the identifier).

Coordinates are PDF user space (y up); a glyph's box is the engine's
character box.
"""

from __future__ import annotations

from typing import NamedTuple

# A drawn line is at most this high, in pt (a stroke's box has no height; a
# filled rule's is its thickness, 0.4 pt in TeX).
LINE_HEIGHT = 2.0
# How near, in pt, an end of a line must be to a glyph's box to touch it
# (sideways), and the line to the glyph's box (in height). A radical's bar
# starts where its sign ends; TeX's underscore starts 0.06 em (0.6 pt at
# 10 pt) right of the letter before it.
TOUCH = 1.0
# At an end of a formula's line, a formula glyph may be this x its size
# further away in height: the numerator or denominator that is as wide as
# the bar sits off it (2409.08861 p51: a 6 pt numerator 2.8 pt above the
# bar; 2405.17880 p3: the bar ends 1.9 pt above the box of the 5 pt
# subscript "t+1" that ends its denominator).
END_REACH = 0.6
# Letters in a row, outside formulas, under or over a line that make it a
# rule over or under text ("min" in a radical is 3).
WORD_LETTERS = 4
# An underscore is at most this x the size long (TeX's is 0.3 em) and lies in
# the bottom part of its glyphs' boxes, this fraction of their height.
UNDERSCORE_WIDTH = 0.6
UNDERSCORE_HEIGHT = 0.3
# The glyphs a line joins into one formula are on one text line: each is
# centred at most this x the size from the line in height (a fraction's
# numerator and denominator are; the next text line, 12 pt away, is not).
SAME_LINE = 1.0
# Two glyphs further apart than this x the size are two words.
WORD_GAP = 0.2


class DrawnLine(NamedTuple):
    """A thin horizontal line drawn on the page, from x0 to x1 at height y."""

    x0: float
    x1: float
    y: float


class Mark(NamedTuple):
    """A glyph as ``owners`` sees it: its box, text, size and whether it is a formula glyph."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    formula: bool
    size: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


class Owners(NamedTuple):
    """The glyphs a drawn line belongs to: ``kind`` is "formula" or "underscore"."""

    kind: str
    indexes: list[int]


def line_of(box) -> DrawnLine | None:
    """The drawn line a shape's box (``x``, ``y``, ``x2``, ``y2``) is, or None."""
    if box is None:
        return None
    height, width = box.y2 - box.y, box.x2 - box.x
    if height > LINE_HEIGHT or width <= height:
        return None
    return DrawnLine(box.x, box.x2, (box.y + box.y2) / 2)


def owners(line: DrawnLine, marks: list[Mark]) -> Owners | None:
    """The glyphs (indexes into ``marks``) the line belongs to, or None (see the module rules)."""
    near = [
        i
        for i, m in enumerate(marks)
        if m.text.strip() and m.y0 - TOUCH <= line.y <= m.y1 + TOUCH
    ]
    left = [i for i in near if marks[i].x0 - TOUCH <= line.x0 <= marks[i].x1 + TOUCH]
    right = [i for i in near if marks[i].x0 - TOUCH <= line.x1 <= marks[i].x1 + TOUCH]
    under = sorted(
        (i for i in near if line.x0 < marks[i].xc < line.x1), key=lambda i: marks[i].x0
    )
    if not under:
        return _underscore(line, marks, left, right) if left and right else None
    if not (_reached(line, marks, line.x0) and _reached(line, marks, line.x1)):
        return None
    if not any(marks[i].formula for i in under + left + right) or _spells_word(marks, under):
        return None
    ends = [i for i in left + right if marks[i].formula and i not in under]
    return Owners("formula", sorted(set(under + ends)))


def _reached(line: DrawnLine, marks: list[Mark], x: float) -> bool:
    """Does an end of a formula's line (at ``x``) touch a glyph? A formula glyph may be
    ``END_REACH`` x its size further away in height (a subscript ending a denominator)."""
    for m in marks:
        if not m.text.strip() or not m.x0 - TOUCH <= x <= m.x1 + TOUCH:
            continue
        reach = TOUCH + (END_REACH * m.size if m.formula else 0.0)
        if m.y0 - reach <= line.y <= m.y1 + reach:
            return True
    return False


def _underscore(line: DrawnLine, marks: list[Mark], left: list[int], right: list[int]):
    before = [i for i in left if abs(marks[i].x1 - line.x0) <= TOUCH and marks[i].text.isalnum()]
    after = [i for i in right if abs(marks[i].x0 - line.x1) <= TOUCH and marks[i].text.isalnum()]
    if not before or not after:
        return None
    a, b = before[0], after[0]
    size = max(marks[a].size, marks[b].size)
    if line.x1 - line.x0 > UNDERSCORE_WIDTH * size:
        return None
    for m in (marks[a], marks[b]):
        if line.y > m.y0 + UNDERSCORE_HEIGHT * (m.y1 - m.y0):
            return None
    return Owners("underscore", [a, b])


def same_word(a: Mark, b: Mark) -> bool:
    """Are ``a`` and then ``b`` letters or digits of one word (no word gap, one baseline)?"""
    return (
        a.text.isalnum()
        and b.text.isalnum()
        and b.x0 - a.x1 <= WORD_GAP * b.size
        and abs(a.y0 - b.y0) <= WORD_GAP * b.size
    )


def _spells_word(marks: list[Mark], under: list[int]) -> bool:
    """Do the glyphs under or over a line spell a word outside formulas?"""
    run = 0
    previous = None
    for i in under:
        m = marks[i]
        joined = previous is not None and m.x0 - previous.x1 <= WORD_GAP * m.size
        if m.text.isalpha() and not m.formula:
            run = run + 1 if joined and run else 1
            if run >= WORD_LETTERS:
                return True
        else:
            run = 0
        previous = m
    return False
