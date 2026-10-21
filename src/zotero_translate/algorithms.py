"""Algorithm detection: find pseudocode by its consecutive ``N:`` line numbers.

The engine's layout model has no algorithm class, so it groups a whole
algorithm into one ordinary paragraph and reflows it. The engine's
paragraph-finding stage calls ``AlgorithmDetector`` once per page, in page
order, with the page's characters; the characters of every detected
algorithm stay out of the ordinary paragraphs, so the engine keeps them as
they are in the source.

Rules (spec "算法识别", user decision D4 = 2 on 2026-10-21):

- Consecutive ``N:`` line numbers are the only necessary condition: at
  least ``MIN_LINES`` numbers that count up by one, aligned on their left or
  right edge, each the first thing in its row.
- Line numbers and line text are joined by their vertical position, not by
  text line: in a line with math the number and the text are separate text
  lines whose baselines differ by about 1.8 pt.
- Rows without a number inside the block (``Require:``/``Ensure:`` rows,
  bold section headings, ``▷`` comments on their own row) do not end the
  algorithm. Rows above line 1 belong to it up to the highest title
  (``Algorithm N``, ``Procedure``) or ``Require``/``Ensure``/``Input``/
  ``Output`` row.
- An ordinary numbered list is not an algorithm: the numbers must be ``N:``
  and the block must contain an algorithm keyword (``for``, ``end for``,
  ``←``, ``Require``, ``return``).
- On the next page, a block whose numbers continue the previous page's
  algorithm and that has no title of its own is another fragment of it.

For each line the detector records the line number, the x where the line
text starts, the baseline and the glyph runs (font, size, bold, small caps),
for line-by-line typesetting. Small caps show up in the text layer as
capitals in two alternating sizes of one font (for example 10 pt and 8 pt).
Coordinates are PDF user space: y grows upwards.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from dataclasses import field

MIN_LINES = 3
KEPT_SOURCE = "kept_source"
# Until line-by-line translation exists (M2 ticket 02), every detected
# algorithm keeps its source text.
NOT_TRANSLATED_YET = "算法逐行翻译尚未实现，整个算法保留原文"

_NUMBER = re.compile(r"^(\d{1,3}):$")
_KEYWORD = re.compile(r"\b(for|return|Require)\b|←")
_TITLE = re.compile(r"^(Algorithm\s*\d+|Procedure\b)")
_HEADER = re.compile(r"^(Algorithm\s*\d+|Procedure\b|Require\b|Ensure\b|Input\b|Output\b)")
# Rows above line 1 that are not a title or Require/Ensure row (a wrapped
# Require row, a bold section heading) may sit between header rows; more
# than this many in a row end the search upwards.
_MAX_PLAIN_HEADER_ROWS = 2


@dataclass
class Glyph:
    """One character as the detector sees it (PDF user space, y up)."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    font: str | None = None
    size: float = 10.0
    bold: bool = False

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def blank(self) -> bool:
        return not self.text or self.text.isspace()


@dataclass
class AlgorithmLine:
    number: int | None
    text: str
    x: float  # where the line text starts (after the line number)
    y: float  # baseline estimate: the median bottom of the line's glyphs
    number_x: float | None = None  # where the line number starts
    runs: list[dict] = field(default_factory=list)

    def report(self) -> dict:
        return {
            "number": self.number,
            "text": self.text,
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "number_x": None if self.number_x is None else round(self.number_x, 1),
            "runs": self.runs,
        }


@dataclass
class AlgorithmFragment:
    page: int  # 1-based
    box: tuple[float, float, float, float]  # x0, y0, x1, y1
    lines: list[AlgorithmLine]
    glyphs: set[int] = field(default_factory=set)  # indexes into the page's characters

    @property
    def numbers(self) -> list[int]:
        return [line.number for line in self.lines if line.number is not None]


@dataclass
class AlgorithmUnit:
    title: str | None
    fragments: list[AlgorithmFragment]
    result: str = KEPT_SOURCE
    reason: str = NOT_TRANSLATED_YET

    @property
    def numbers(self) -> list[int]:
        return [n for fragment in self.fragments for n in fragment.numbers]

    def report(self) -> dict:
        numbers = self.numbers
        return {
            "pages": [fragment.page for fragment in self.fragments],
            "title": self.title,
            "numbered_lines": len(numbers),
            "first_line": numbers[0],
            "last_line": numbers[-1],
            "result": self.result,
            "reason": self.reason,
            "fragments": [
                {
                    "page": fragment.page,
                    "box": [round(v, 1) for v in fragment.box],
                    "lines": [line.report() for line in fragment.lines],
                }
                for fragment in self.fragments
            ],
        }


class AlgorithmDetector:
    """Detects algorithms page by page; ``units`` holds every algorithm found."""

    def __init__(self) -> None:
        self.units: list[AlgorithmUnit] = []

    @property
    def kept_source(self) -> int:
        return sum(1 for unit in self.units if unit.result == KEPT_SOURCE)

    def report(self) -> list[dict]:
        return [unit.report() for unit in self.units]

    def engine_page(self, page) -> set[int]:
        """The engine hook: indexes in ``page.pdf_character`` of the page's pseudocode."""
        return self(page.page_number + 1, glyphs_of_page(page))

    def __call__(self, page_number: int, glyphs: list[Glyph]) -> set[int]:
        """Detect the algorithms on one page; return the indexes of their glyphs."""
        found = detect_page(page_number, glyphs)
        taken: set[int] = set()
        for index, (title, fragment) in enumerate(found):
            previous = self.units[-1] if self.units else None
            if (
                index == 0
                and title is None
                and previous is not None
                and previous.fragments[-1].page == page_number - 1
                and fragment.numbers[0] == previous.numbers[-1] + 1
            ):
                previous.fragments.append(fragment)
            else:
                self.units.append(AlgorithmUnit(title, [fragment]))
            taken |= fragment.glyphs
        return taken


def glyphs_of_page(page) -> list[Glyph]:
    """The detector's view of an engine page (intermediate representation)."""
    fonts = {font.font_id: font for font in page.pdf_font or []}
    for xobject in page.pdf_xobject or []:
        for font in xobject.pdf_font or []:
            fonts.setdefault(font.font_id, font)
    glyphs = []
    for char in page.pdf_character:
        # The font box, not the visual box: punctuation and letters on one
        # baseline then share a vertical centre.
        box = char.box or char.visual_bbox.box
        style = char.pdf_style
        font = fonts.get(style.font_id) if style else None
        name = font.name if font else None
        glyphs.append(
            Glyph(
                text=char.char_unicode or "",
                x0=box.x,
                y0=box.y,
                x1=box.x2,
                y1=box.y2,
                font=name or (style.font_id if style else None),
                size=(style.font_size if style and style.font_size else 10.0),
                bold=bool(font and (font.bold or "bold" in (name or "").lower())),
            )
        )
    return glyphs


def detect_page(page_number: int, glyphs: list[Glyph]) -> list[tuple[str | None, AlgorithmFragment]]:
    """The algorithm fragments on one page, top to bottom, with their titles."""
    found = []
    used: set[int] = set()
    for chain in _number_chains(glyphs):
        fragment, title = _fragment(page_number, glyphs, chain, used)
        if fragment is None:
            continue
        used |= fragment.glyphs
        found.append((fragment.box[3], title, fragment))
    found.sort(key=lambda item: -item[0])
    return [(title, fragment) for _, title, fragment in found]


@dataclass
class _Number:
    value: int
    glyphs: list[int]
    x0: float
    x1: float
    yc: float
    size: float


def _rows(glyphs: list[Glyph], indexes, tolerance: float) -> list[list[int]]:
    """Group glyphs into rows by their vertical centre, top row first."""
    ordered = sorted(indexes, key=lambda i: -glyphs[i].yc)
    rows: list[list[int]] = []
    for i in ordered:
        if rows and glyphs[rows[-1][0]].yc - glyphs[i].yc <= tolerance:
            rows[-1].append(i)
        else:
            rows.append([i])
    for row in rows:
        row.sort(key=lambda i: glyphs[i].x0)
    return rows


def _line_numbers(glyphs: list[Glyph]) -> list[_Number]:
    """Every ``N:`` token that starts its row (nothing just to its left)."""
    sizes = [g.size for g in glyphs if not g.blank]
    if not sizes:
        return []
    tolerance = 0.3 * statistics.median(sizes)
    numbers = []
    for row in _rows(glyphs, [i for i, g in enumerate(glyphs) if not g.blank], tolerance):
        # Split the row into tokens at gaps wider than a quarter of the glyph size.
        tokens: list[list[int]] = []
        for i in row:
            g = glyphs[i]
            if tokens and g.x0 - glyphs[tokens[-1][-1]].x1 <= 0.25 * g.size:
                tokens[-1].append(i)
            else:
                tokens.append([i])
        for k, token in enumerate(tokens):
            text = "".join(glyphs[i].text for i in token)
            match = _NUMBER.match(text)
            if not match:
                continue
            first = glyphs[token[0]]
            if k > 0 and first.x0 - glyphs[tokens[k - 1][-1]].x1 < 6:
                continue  # "Figure 10:" and the like
            numbers.append(
                _Number(
                    int(match.group(1)),
                    token,
                    first.x0,
                    glyphs[token[-1]].x1,
                    statistics.mean(glyphs[i].yc for i in token),
                    max(glyphs[i].size for i in token),
                )
            )
    return numbers


def _number_chains(glyphs: list[Glyph]) -> list[list[_Number]]:
    """Runs of line numbers that count up by one, aligned, top to bottom."""
    chains: list[list[_Number]] = []
    for number in sorted(_line_numbers(glyphs), key=lambda n: -n.yc):
        for chain in chains:
            last = chain[-1]
            drop = last.yc - number.yc
            if (
                number.value == last.value + 1
                and (abs(number.x1 - last.x1) <= 3 or abs(number.x0 - last.x0) <= 3)
                and 0 < drop <= 5 * max(last.size, number.size)
            ):
                chain.append(number)
                break
        else:
            chains.append([number])
    return [chain for chain in chains if len(chain) >= MIN_LINES]


def _reach(glyphs: list[Glyph], row: list[int], start: float, gap: float) -> float:
    """How far right a row runs from ``start`` without a column gap; ``▷`` bridges gaps."""
    right = start
    for i in row:
        g = glyphs[i]
        if g.x1 <= right:
            continue
        if g.x0 - right > gap and g.text != "▷":
            break
        right = max(right, g.x1)
    return right


def _fragment(page_number: int, glyphs: list[Glyph], chain: list[_Number], used: set[int]):
    pitch = min(a.yc - b.yc for a, b in zip(chain, chain[1:]))
    half = 0.5 * pitch
    left = min(n.x0 for n in chain) - 1
    top = chain[0].yc + half
    bottom = chain[-1].yc - half
    size = statistics.median(n.size for n in chain)
    free = [i for i, g in enumerate(glyphs) if i not in used and not g.blank]

    # Right edge: as far as the numbered rows run without crossing a column
    # gap (the gap after the line number is indentation, not a column gap),
    # and the comment rows (``▷``) and headings between them.
    number_glyphs = {i for n in chain for i in n.glyphs}
    numbers_right = max(n.x1 for n in chain)
    gap = 2.5 * size
    band = [i for i in free if bottom <= glyphs[i].yc <= top and glyphs[i].xc >= left]
    rows = _rows(glyphs, band, 0.3 * pitch)
    body_starts = [
        glyphs[row[k]].x0
        for row in rows
        for k in range(len(row))
        if row[k] not in number_glyphs and any(i in number_glyphs for i in row[:k])
    ]
    deepest = max(body_starts, default=numbers_right) + gap
    right = numbers_right
    for row in rows:
        body = [i for i in row if i not in number_glyphs and glyphs[i].x0 >= numbers_right - 0.5]
        if not body:
            continue
        first = glyphs[body[0]]
        if first.text != "▷" and first.x0 > deepest:
            continue  # a row of another column
        right = max(right, _reach(glyphs, body, first.x0, gap))

    # Header rows above line 1: title, Require/Ensure, and what lies between.
    above = [
        i
        for i in free
        if top < glyphs[i].yc <= top + 6 * pitch and glyphs[i].xc >= left - 4
    ]
    header_rows: list[list[int]] = []
    plain = 0
    edge = top
    title = None
    for row in reversed(_rows(glyphs, above, 0.3 * pitch)):
        # A header row ends at the first column gap.
        reach = _reach(glyphs, row, glyphs[row[0]].x0, gap)
        row = [i for i in row if glyphs[i].x1 <= reach + 0.5]
        row_top = max(glyphs[i].yc for i in row)
        if row_top - edge > 2.5 * pitch:
            break
        text = _row_text(glyphs, row)
        if _HEADER.match(text):
            header_rows.append(row)
            plain = 0
            if _TITLE.match(text):
                title = text
        else:
            plain += 1
            if plain > _MAX_PLAIN_HEADER_ROWS:
                break
            header_rows.append(row)
        edge = row_top
    while header_rows and not _HEADER.match(_row_text(glyphs, header_rows[-1])):
        header_rows.pop()
    header_glyphs = {i for row in header_rows for i in row}
    if header_rows:
        top = max(glyphs[i].yc for i in header_rows[-1]) + 0.3 * pitch
        left = min(left, min(glyphs[i].x0 for i in header_glyphs) - 1)

    members = {
        i for i in free if bottom <= glyphs[i].yc <= top and left <= glyphs[i].xc <= right + 1
    } | header_glyphs
    members |= {i for n in chain for i in n.glyphs}
    lines = _lines(glyphs, members, chain, pitch)
    # Match keywords on the line texts, which have spaces between words.
    if not _KEYWORD.search("\n".join(line.text for line in lines)):
        return None, None
    box = (
        min(glyphs[i].x0 for i in members),
        min(glyphs[i].y0 for i in members),
        max(glyphs[i].x1 for i in members),
        max(glyphs[i].y1 for i in members),
    )
    # Blank glyphs (spaces) inside the box belong to the algorithm too.
    members |= {
        i
        for i, g in enumerate(glyphs)
        if g.blank and i not in used and box[0] <= g.xc <= box[2] and box[1] <= g.yc <= box[3]
    }
    return AlgorithmFragment(page_number, box, lines, members), title


def _lines(glyphs: list[Glyph], members: set[int], chain: list[_Number], pitch: float) -> list[AlgorithmLine]:
    """Join each line number with the glyphs at its height; the rest form unnumbered rows."""
    number_glyphs = {i: n for n in chain for i in n.glyphs}
    numbered: dict[int, list[int]] = {n.value: [] for n in chain}
    small = statistics.median(glyphs[i].size for i in members)
    rest = []
    for i in members:
        if i in number_glyphs:
            continue
        nearest = min(chain, key=lambda n: abs(n.yc - glyphs[i].yc))
        distance = abs(nearest.yc - glyphs[i].yc)
        # Superscripts and subscripts sit further from the line's centre.
        if distance <= 0.4 * pitch or (glyphs[i].size < 0.85 * small and distance <= 0.75 * pitch):
            numbered[nearest.value].append(i)
        else:
            rest.append(i)
    placed = [(n.yc, _line(glyphs, n.value, n.x0, numbered[n.value], n.yc)) for n in chain]
    for row in _rows(glyphs, rest, 0.3 * pitch):
        yc = statistics.mean(glyphs[i].yc for i in row)
        placed.append((yc, _line(glyphs, None, None, row, yc)))
    placed.sort(key=lambda item: -item[0])
    return [line for _, line in placed]


def _line(glyphs: list[Glyph], number, number_x, body: list[int], yc: float) -> AlgorithmLine:
    body = sorted(body, key=lambda i: glyphs[i].x0)
    text_glyphs = [i for i in body if not glyphs[i].blank]
    return AlgorithmLine(
        number=number,
        text=_row_text(glyphs, body),
        x=glyphs[text_glyphs[0]].x0 if text_glyphs else (number_x or 0.0),
        y=statistics.median(glyphs[i].y0 for i in text_glyphs) if text_glyphs else yc,
        number_x=number_x,
        runs=_runs(glyphs, body),
    )


def _row_text(glyphs: list[Glyph], row: list[int]) -> str:
    """The row's text, with a space where glyphs are a word gap apart."""
    parts = []
    previous = None
    for i in sorted(row, key=lambda i: glyphs[i].x0):
        g = glyphs[i]
        if g.blank:
            continue
        if previous is not None and g.x0 - previous.x1 > 0.2 * g.size:
            parts.append(" ")
        parts.append(g.text)
        previous = g
    return "".join(parts)


def _runs(glyphs: list[Glyph], body: list[int]) -> list[dict]:
    """Glyph runs of one line: same font, size, bold and small caps."""
    chars = [glyphs[i] for i in sorted(body, key=lambda i: glyphs[i].x0) if not glyphs[i].blank]
    # Words: glyphs a word gap apart start a new word.
    words: list[list[Glyph]] = []
    for g in chars:
        if words and g.x0 - words[-1][-1].x1 <= 0.2 * g.size and g.font == words[-1][-1].font:
            words[-1].append(g)
        else:
            words.append([g])
    runs: list[dict] = []
    for word in words:
        small_caps = _small_caps(word)
        pieces = [word] if small_caps else [[g] for g in word]
        for piece in pieces:
            g = piece[0]
            size = round(max(p.size for p in piece), 1)
            key = (g.font, size, g.bold, small_caps)
            gap = bool(runs) and piece is pieces[0]
            if runs and runs[-1]["_key"] == key:
                runs[-1]["text"] += (" " if gap else "") + "".join(p.text for p in piece)
            else:
                runs.append(
                    {
                        "_key": key,
                        "text": "".join(p.text for p in piece),
                        "font": g.font,
                        "size": size,
                        "bold": g.bold,
                        "small_caps": small_caps,
                    }
                )
    for run in runs:
        del run["_key"]
    return runs


def _small_caps(word: list[Glyph]) -> bool:
    letters = [g for g in word if g.text.isalpha()]
    sizes = {round(g.size, 1) for g in letters}
    return (
        len(letters) >= 2
        and len(sizes) >= 2
        and all(g.text.isupper() for g in letters)
        and len({g.font for g in letters}) == 1
    )
