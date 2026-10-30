"""Algorithm detection: find pseudocode by its consecutive line numbers, or
without line numbers by the rules drawn around it.

The engine's layout model has no algorithm class, so it groups a whole
algorithm into one ordinary paragraph and reflows it. The engine's
paragraph-finding stage calls ``AlgorithmDetector`` once per page, in page
order, with the page's characters and horizontal rules; the characters of
every detected algorithm stay out of the ordinary paragraphs, so the engine
keeps them as they are in the source.

Rules for numbered algorithms (M2 spec "算法识别", user decision D4 = 2 on
2026-10-21; closure spec "行号规则" and "算法的栏界", ticket 08):

- Consecutive line numbers are the only necessary condition: at least
  ``MIN_LINES`` numbers written the same way that count up by one, aligned
  on their left or right edge, each the first thing in its row, at most
  ``_MAX_DROP`` rows of text apart.
- Line numbers are written ``N:``, ``N.`` (algorithmic styles; "1." may run
  into the line's text, but not into a digit: "3.4" is a section number) or
  a bare ``N`` (algorithm2e, smaller than the text, sometimes in the gap
  beside the text's column). ``N.`` and ``N`` also need a title row
  (``Algorithm N``, ``Procedure``) above line 1, so an ordinary list
  numbered "1.", "2." is not an algorithm; a sentence that cites
  "Algorithm 1" does not start its row with it, so it is no title. ``N.``
  steps may be indented with a loop (up to 2 x the size). Drawn rules are
  not used for numbered algorithms: they are found by characters only.
- The algorithm stays in the column of its line numbers, header rows
  included: the first x range right of the most indented numbered line, at
  least the numbers' size wide, that no glyph of the numbered rows crosses,
  with text right of it at the same height (not a ``▷`` comment), is the
  gap to another column; beside another column the algorithm takes its
  whole column (``_other_column``).
- A code listing is not an algorithm: most glyphs of its numbered lines are
  in a code font, or (in a font without a name) its letters are monospaced.
- Line numbers and line text are joined by their vertical position, not by
  text line: in a line with math the number and the text are separate text
  lines whose baselines differ by about 1.8 pt.
- Rows without a number inside the block (``Require:``/``Ensure:`` rows,
  bold section headings, ``▷`` comments on their own row) do not end the
  algorithm. Rows above line 1 belong to it up to the highest title
  (``Algorithm N``, ``Procedure``) or ``Require``/``Ensure``/``Input``/
  ``Output`` row.
- An ordinary numbered list is not an algorithm: the block must contain an
  algorithm keyword (``for``, ``end for``, ``←``, ``Require``, ``return``),
  and ``N.`` or ``N`` numbers a title.
- On the next page, a block whose numbers continue the previous page's
  algorithm and that has no title of its own is another fragment of it.

Each algorithm is found in two steps: its anchors (the chain of line
numbers) give a band, the rows of the numbered lines (``_band``); the band
gives the column bounds, the header rows above line 1 and the lines
(``_fragment``).

Rules for algorithms without line numbers (closure spec "无行号算法规则",
ticket 12; 11 of the held-out set's 13 are ``algorithmic`` without line
numbers or algorithm2e ruled floats):

- The anchors are three horizontal rules with the same ends (within
  ``_RULE_ALIGN``): one above the title row, one below it, one at the end
  of the algorithm (``_ruled_boxes``). The first row between the first two
  is a title (``Algorithm N``, ``Procedure``), at most ``_TITLE_BAND`` x its
  size high. Below the title there are at least ``MIN_LINES`` rows with an
  algorithm keyword, not a code listing (2605.03984 p4: Python in NimbusMonL
  under "Algorithm 1"). The rules give the column bounds: only glyphs
  between them, centred at most ``_RULE_SLACK`` beyond their ends, belong
  to the algorithm (``_ruled_fragment``).
- Not algorithms: a paragraph that starts with "Algorithm 1" (no rules), a
  table between rules (no title row between the first two), and an area a
  numbered algorithm has taken. An algorithm written as a paragraph
  (2608.03223 p12: title and "(1)" steps run in a paragraph, no rules), an
  algorithm without rules, and one without its end rule on the page (it
  runs on to the next page) are not found.
- Rows (``_unnumbered_lines``): the glyphs of text fonts (letters and
  digits, not math fonts, not smaller than ``_SCRIPT`` x the median size)
  form the anchor rows, and so do glyphs of full size far from every text
  row (rows of formulas only); formulas, sub- and superscripts join the
  anchor row nearest by height, by the distances of the numbered case
  (``_by_number``, the pitch being the median distance between text rows);
  what is near no anchor row forms rows of its own. A part of a text row
  right of it after a column gap without its anchor glyphs is not the row's
  (``_strays``); a big delimiter or radical (math extension font) touching a
  text row's formula sideways is (it must move with the formula). Every row
  has no line number.

For each line the detector records the line number, the x where the line
text starts, the baseline and the glyph runs (font, size, bold, small caps),
for line-by-line typesetting. It also marks which glyphs of the line keep
their source text when the line is translated (``keep_flags``): formulas
(math fonts), keywords set in bold, the title's ``Algorithm N`` and the
``Require:``/``Ensure:``/``Input:``/``Output:`` labels, function calls
(small caps names) with everything inside their parentheses (user decision
D5 = 1 on 2026-10-21), and constants and identifiers (ticket 16: small caps
by font name, words of capitals only, underscores and camelCase; the rule
is in ``algorithm_translation``'s module documentation). Small caps show up
in the text layer as capitals in two alternating sizes of one font (for
example 10 pt and 8 pt), or as ordinary letters in a small caps font.
Coordinates are PDF user space: y grows upwards.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from dataclasses import field
from typing import NamedTuple

MIN_LINES = 3
KEPT_SOURCE = "kept_source"
TRANSLATED = "translated"
# An algorithm keeps its source text until its translation is typeset.
NOT_TRANSLATED = "算法没有经过逐行翻译，整个算法保留原文"

# A line number token: "N:", "N." or a bare "N". "N." may run into the
# line's text ("1.Sample": the space is narrow), but not into a digit
# ("3.4" is a section number).
_NUMBER = re.compile(r"^(\d{1,3})(:$|\.(?!\d)|$)")
_KEYWORD = re.compile(r"\b(for|return|Require)\b|←")
_TITLE = re.compile(r"^(Algorithm\s*\d+|Procedure\b)")
_HEADER = re.compile(r"^(Algorithm\s*\d+|Procedure\b|Require\b|Ensure\b|Input\b|Output\b)")
# The keywords the translation prompt names: they stay in English, even as
# plain text. Only these go into the prompt.
PROMPT_KEYWORDS = (
    "for", "for each", "for all", "while", "do", "if", "then", "else", "else if", "end",
    "end for", "end if", "end while", "repeat", "until", "return", "to", "in",
    "Require", "Ensure", "Input", "Output", "function", "procedure",
)
# Words that keep their source text when set in bold: the prompt keywords'
# words and words that are keywords only in bold ("and" in plain text is
# natural language to translate).
KEYWORDS = frozenset(word.lower() for phrase in PROMPT_KEYWORDS for word in phrase.split()) | {
    "forall", "elseif", "elsif", "downto", "and", "or", "not", "break", "continue", "loop", "parallel",
}
_LABEL = re.compile(r"^(Algorithm\d+[:.]?|Procedure|Require:|Ensure:|Input:|Output:)")
_MATH_FONT = re.compile(
    r"(?:^|\+)(?:CM[A-Z]{1,5}\d|MSBM|MSAM|EU[FRSX]M|RSFS|STIX\w*Math|LatinModernMath|CambriaMath|Symbol"
    r"|LMMathItalic|NewTXMI)"
)
_CODE_FONT = re.compile(
    r"Courier|Mono|NimbusMon|Typewriter|Inconsolata|(?:^|\+)(?:CMTT|SFTT)", re.IGNORECASE
)
# Fonts of big delimiters and radicals (TeX's math extension fonts).
_DELIMITER_FONT = re.compile(r"CMEX|EUEX|Extension", re.IGNORECASE)
# Small caps fonts by name (ticket 16): cm-super (SFCC, AutoDesign p7
# "Accept"), EC (ECCC), Computer Modern (CMCSC, also a math font above),
# Latin Modern (LMRomanCaps). Their text layer reads "Accept", not
# capitals in two sizes. A name ending in "SC" is not one: PingFangSC,
# NotoSansSC are Simplified Chinese.
_SMALL_CAPS_FONT = re.compile(r"(?:^|\+)(?:SFCC|ECCC|CMCSC)\d|LMRomanCaps|SmallCaps")
# A word of capitals only (no lower case) with at least this many letters is
# a constant (ACCEPT, REJECT); a shorter one is an abbreviation that the
# glossary may translate (LLM, MCTS, DDPM, FAISS, RAPSD: at most 5 letters
# in the held-out set's algorithms).
_CONSTANT_LETTERS = 6
COMMENT_MARK = "▷"
_RANGE_WORDS = frozenset({"to", "downto", "step", "by"})

# Rows above line 1 that are not a title or Require/Ensure row (a wrapped
# Require row, a bold section heading) may sit between header rows; more
# than this many in a row end the search upwards.
_MAX_PLAIN_HEADER_ROWS = 2
# Distances as fractions of a size or of the pitch (the smallest distance
# between two numbered rows):
_WORD_GAP = 0.2  # a gap wider than this x the glyph's size is a word gap
_ROW = 0.3  # glyphs whose centres are this x the pitch (or median size) apart or less share a row
# A gap wider than this x the numbers' size ends a row: another column.
# Without line numbers, the same x the median size splits off the part of a
# text row right of such a gap that holds none of its anchor glyphs
# (``_strays``).
_COLUMN_GAP = 2.5
# An x range at least this x the numbers' size wide that no glyph of the
# numbered rows crosses, with text right of it at the same height, is the
# gap to another column (``_other_column``).
_MIN_COLUMN_GAP = 1.0
# A glyph belongs to the numbered line whose number is nearest by height
# when it is at most _NEAR x the pitch from it; a sub- or superscript
# (smaller than _SCRIPT x the median size) when at most _NEAR_SCRIPT x.
# Without line numbers the anchor rows play the numbers' part, and a big
# delimiter or radical joins a text row at most _NEAR_SCRIPT x the pitch
# away whose formula glyphs it touches (within _TOUCH pt sideways).
_NEAR = 0.4
_NEAR_SCRIPT = 0.75
_TOUCH = 1.0
_SCRIPT = 0.85
# "N." steps may be indented with a loop: two numbers of a chain may start
# this x their size apart (2602.03211 p4: 10 pt at 10 pt).
_STEP_INDENT = 2
# A number just right of text (closer than 6 pt) is part of that text
# ("Figure 10:") unless the text is more than this x the number's size:
# algorithm2e's smaller numbers in the gap beside the text's column.
_GUTTER_NUMBER_RATIO = 1.25
# Monospaced letters (``_code_listing``): at least _MONO_LETTERS letters, of
# which _MONO_SHARE have a width/size within _MONO_TOLERANCE of the median.
_MONO_LETTERS = 20
_MONO_SHARE = 0.9
_MONO_TOLERANCE = 0.03
# Two numbered rows of one algorithm are at most this x the size apart
# (2412.16318: display formulas put 73 pt between lines 7 and 8 of 10 pt
# text on p27, 68.7 pt between lines 12 and 13 on p41).
_MAX_DROP = 8
# Horizontal rules (algorithms without line numbers): a stroke or filled box
# less than _RULE_HEIGHT pt high and at least _MIN_RULE pt long is a rule;
# the three rules of one algorithm end within _RULE_ALIGN pt of each other;
# its glyphs are centred at most _RULE_SLACK pt beyond the rules' ends
# (2603.19222 p4: a "▷" comment ends 0.7 pt right of the rules); the first
# two rules are at most _TITLE_BAND x the title's size apart (13.4-15.2 pt
# at 10 pt in the held-out set).
_RULE_HEIGHT = 2.0
_MIN_RULE = 20.0
_RULE_ALIGN = 2.0
_RULE_SLACK = 2.0
_TITLE_BAND = 3.0


class Rule(NamedTuple):
    """A horizontal rule drawn on the page, from x0 to x1 at height y (PDF user space)."""

    x0: float
    x1: float
    y: float


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
    # The line's glyphs without the line number and blanks, left to right;
    # their indexes in the page's characters; whether each keeps its source
    # text; and (set by ``AlgorithmDetector.engine_page``) the engine's
    # characters for them.
    glyphs: list[Glyph] = field(default_factory=list, repr=False)
    indexes: list[int] = field(default_factory=list, repr=False)
    keep: list[bool] = field(default_factory=list, repr=False)
    chars: list = field(default_factory=list, repr=False)

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
    chars: list = field(default_factory=list, repr=False)  # the engine's characters (engine_page)

    @property
    def numbers(self) -> list[int]:
        return [line.number for line in self.lines if line.number is not None]


@dataclass
class AlgorithmUnit:
    title: str | None
    fragments: list[AlgorithmFragment]
    result: str = KEPT_SOURCE
    reason: str | None = NOT_TRANSLATED
    # Each translated row: {"line", "part", "source", "output"} (set by the translator).
    translations: list[dict] = field(default_factory=list)

    @property
    def numbers(self) -> list[int]:
        return [n for fragment in self.fragments for n in fragment.numbers]

    def keep_source(self, reason: str) -> None:
        """The whole algorithm keeps its source text, for ``reason`` (in the task report)."""
        self.result = KEPT_SOURCE
        self.reason = reason

    def mark_translated(self) -> None:
        """The algorithm's translation is typeset on every page it is on."""
        self.result = TRANSLATED
        self.reason = None

    def report(self) -> dict:
        numbers = self.numbers
        return {
            "pages": [fragment.page for fragment in self.fragments],
            "title": self.title,
            "numbered_lines": len(numbers),
            "first_line": numbers[0] if numbers else None,
            "last_line": numbers[-1] if numbers else None,
            "result": self.result,
            "reason": self.reason,
            "translations": self.translations,
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
        known = {id(f) for unit in self.units for f in unit.fragments}
        taken = self(page.page_number + 1, glyphs_of_page(page), rules_of_page(page))
        for unit in self.units:
            for fragment in unit.fragments:
                if id(fragment) in known:
                    continue
                fragment.chars = [page.pdf_character[i] for i in sorted(fragment.glyphs)]
                for line in fragment.lines:
                    line.chars = [page.pdf_character[i] for i in line.indexes]
        return taken

    def __call__(self, page_number: int, glyphs: list[Glyph], rules=()) -> set[int]:
        """Detect the algorithms on one page (``rules``: its horizontal rules, ``Rule`` or
        ``(x0, x1, y)``); return the indexes of their glyphs."""
        found = detect_page(page_number, glyphs, rules)
        taken: set[int] = set()
        for index, (title, fragment) in enumerate(found):
            previous = self.units[-1] if self.units else None
            if (
                index == 0
                and title is None
                and previous is not None
                and previous.fragments[-1].page == page_number - 1
                and fragment.numbers
                and previous.numbers
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


def rules_of_page(page) -> list[Rule]:
    """The horizontal rules of an engine page: thin strokes and filled boxes (``Rule``)."""
    rules = []
    for shape in [*(page.pdf_curve or []), *(page.pdf_rectangle or [])]:
        box = shape.box
        if box is not None and box.y2 - box.y < _RULE_HEIGHT and box.x2 - box.x >= _MIN_RULE:
            rules.append(Rule(box.x, box.x2, (box.y + box.y2) / 2))
    return rules


def detect_page(
    page_number: int, glyphs: list[Glyph], rules=()
) -> list[tuple[str | None, AlgorithmFragment]]:
    """The algorithm fragments on one page, top to bottom, with their titles.

    Numbered algorithms first; then algorithms without line numbers between
    rules, where no numbered algorithm is.
    """
    found = []
    used: set[int] = set()
    for chain in _number_chains(glyphs):
        detected = _fragment(page_number, glyphs, _band(chain), used)
        if detected is None:
            continue
        used |= detected[1].glyphs
        found.append(detected)
    for box in _ruled_boxes([Rule(*rule) for rule in rules]):
        detected = _ruled_fragment(page_number, glyphs, box, used)
        if detected is None:
            continue
        used |= detected[1].glyphs
        found.append(detected)
    found.sort(key=lambda item: -item[1].box[3])
    return found


def _ruled_boxes(rules: list[Rule]) -> list[tuple[Rule, Rule, Rule]]:
    """Rules with the same ends three in a row, top to bottom: above the title, below it, the end."""
    rules = sorted(rules, key=lambda r: -r.y)
    distinct: list[Rule] = []
    for rule in rules:  # a rule drawn twice (stroke and fill) is one
        if not any(_same_ends(rule, r) and abs(rule.y - r.y) < _RULE_HEIGHT for r in distinct):
            distinct.append(rule)
    boxes = []
    for k, top in enumerate(distinct):
        lower = [r for r in distinct[k + 1 :] if _same_ends(r, top)]
        if len(lower) >= 2:
            boxes.append((top, lower[0], lower[1]))
    return boxes


def _same_ends(a: Rule, b: Rule) -> bool:
    return abs(a.x0 - b.x0) <= _RULE_ALIGN and abs(a.x1 - b.x1) <= _RULE_ALIGN


def _ruled_fragment(
    page_number: int, glyphs: list[Glyph], box: tuple[Rule, Rule, Rule], used: set[int]
) -> tuple[str, AlgorithmFragment] | None:
    """An algorithm without line numbers between three rules, or None (see the module rules)."""
    top, below, end = box
    left, right = min(r.x0 for r in box) - _RULE_SLACK, max(r.x1 for r in box) + _RULE_SLACK
    inside = [
        i
        for i, g in enumerate(glyphs)
        if not g.blank and left <= g.xc <= right and end.y < g.yc < top.y
    ]
    if not inside or any(i in used for i in inside):
        return None  # nothing there, or a numbered algorithm's area
    title_glyphs = [i for i in inside if glyphs[i].yc > below.y]
    if not title_glyphs:
        return None
    size = statistics.median(glyphs[i].size for i in title_glyphs)
    title_row = _rows(glyphs, title_glyphs, _ROW * size)[0]
    title = _row_text(glyphs, title_row)
    if not is_title(title) or top.y - below.y > _TITLE_BAND * size:
        return None
    lines = _unnumbered_lines(glyphs, inside)
    body = [line for line in lines if line.y < below.y]
    if len(body) < MIN_LINES:
        return None
    if not _KEYWORD.search("\n".join(line.text for line in body)) or _code_listing(body):
        return None
    members = set(inside)
    bounds = (
        min(glyphs[i].x0 for i in members),
        min(glyphs[i].y0 for i in members),
        max(glyphs[i].x1 for i in members),
        max(glyphs[i].y1 for i in members),
    )
    members |= {
        i
        for i, g in enumerate(glyphs)
        if g.blank
        and i not in used
        and bounds[0] <= g.xc <= bounds[2]
        and bounds[1] <= g.yc <= bounds[3]
    }
    return title, AlgorithmFragment(page_number, bounds, lines, members)


def _unnumbered_lines(glyphs: list[Glyph], members: list[int]) -> list[AlgorithmLine]:
    """The rows of an algorithm without line numbers, top to bottom (see the module rules)."""
    small = statistics.median(glyphs[i].size for i in members)
    text = [
        i
        for i in members
        if glyphs[i].size >= _SCRIPT * small
        and glyphs[i].text.isalnum()
        and not _MATH_FONT.search(glyphs[i].font or "")
    ]
    anchors = _rows(glyphs, text, _ROW * small)
    centres = [statistics.mean(glyphs[i].yc for i in row) for row in anchors]
    distances = [a - b for a, b in zip(centres, centres[1:])]
    pitch = statistics.median(distances) if distances else small
    # Rows of formulas only (2408.07199 p11: "π_ref ← π_θi"): their glyphs of
    # full size far from every text row are anchors too, so that their sub-
    # and superscripts do not join the text row below.
    formula = [
        i
        for i in members
        if glyphs[i].size >= _SCRIPT * small
        and all(abs(glyphs[i].yc - yc) > _NEAR * pitch for yc in centres)
    ]
    centres += [statistics.mean(glyphs[i].yc for i in row) for row in _rows(glyphs, formula, _ROW * small)]
    band = _Band(
        numbers=[_Number(k, [], 0.0, 0.0, yc, small) for k, yc in enumerate(centres)],
        pitch=pitch,
        size=small,
        left=0.0,
        top=0.0,
        bottom=0.0,
    )
    joined, rest = _by_number(glyphs, members, band, small) if centres else ({}, list(members))
    # A piece of a formula of the next row that reaches up beside a text row,
    # right of it after a column gap (2409.08861 p11: the radical of row 5
    # beside the "for" row), is not part of the row: it would move with the
    # row's translated text.
    for value, row in joined.items():
        if value >= len(anchors):
            continue  # a row of formulas only: nothing in it moves
        strays = _strays(glyphs, row, set(anchors[value]), _COLUMN_GAP * small)
        joined[value] = [i for i in row if i not in strays]
        rest += sorted(strays)
    # A big delimiter or radical of a text row's formula, set apart by height
    # (2412.16318 p7: the parentheses of "Õ(T^{2/3})" 8 pt above "Play bad
    # arms"; 2409.08861 p11: the radical beside the "for" row above), joins
    # that row when it touches the row's formula glyphs sideways, so that it
    # moves with them.
    loose = rest + [i for value, row in joined.items() if value >= len(anchors) for i in row]
    loose = [i for i in loose if _DELIMITER_FONT.search(glyphs[i].font or "")]
    text = set(text)
    for i in loose:
        g = glyphs[i]
        near = [
            value
            for value in range(len(anchors))
            if abs(g.yc - centres[value]) <= _NEAR_SCRIPT * pitch
            and any(
                j not in text and glyphs[j].x0 - _TOUCH <= g.x1 and g.x0 <= glyphs[j].x1 + _TOUCH
                for j in joined.get(value, [])
            )
        ]
        if near:
            value = min(near, key=lambda v: abs(g.yc - centres[v]))
            for row in [rest, *joined.values()]:
                if i in row:
                    row.remove(i)
            joined[value].append(i)
    placed = [
        (n.yc, _line(glyphs, None, None, joined[n.value], n.yc)) for n in band.numbers if n.value in joined
    ]
    for row in _rows(glyphs, rest, _ROW * band.pitch):
        yc = statistics.mean(glyphs[i].yc for i in row)
        placed.append((yc, _line(glyphs, None, None, row, yc)))
    placed.sort(key=lambda item: -item[0])
    return [line for _, line in placed if line.glyphs]


def _strays(glyphs: list[Glyph], row: list[int], anchored: set[int], gap: float) -> set[int]:
    """The row's glyphs right of its last piece with an anchor glyph, pieces split at gaps wider than ``gap``."""
    pieces: list[list[int]] = []
    right = None
    for i in sorted(row, key=lambda i: glyphs[i].x0):
        if right is None or glyphs[i].x0 - right > gap:
            pieces.append([])
        pieces[-1].append(i)
        right = glyphs[i].x1 if right is None else max(right, glyphs[i].x1)
    last = max((k for k, piece in enumerate(pieces) if any(i in anchored for i in piece)), default=len(pieces))
    return {i for piece in pieces[last + 1 :] for i in piece}


@dataclass
class _Number:
    value: int
    glyphs: list[int]
    x0: float
    x1: float
    yc: float
    size: float
    style: str = ":"  # ":", "." or "" (a bare number)
    row_size: float = 0.0  # the median glyph size of the number's row


@dataclass
class _Band:
    """The rows of an algorithm's numbered lines, found from its line numbers (the anchors)."""

    numbers: list[_Number]  # top to bottom
    pitch: float  # the smallest distance between two numbered rows
    size: float  # the numbers' median size
    left: float
    top: float
    bottom: float
    style: str = ":"  # the numbers' style


def _band(chain: list[_Number]) -> _Band:
    """Step 1, anchors to band: where the numbered rows are."""
    pitch = min(a.yc - b.yc for a, b in zip(chain, chain[1:]))
    return _Band(
        numbers=chain,
        pitch=pitch,
        size=statistics.median(n.size for n in chain),
        left=min(n.x0 for n in chain) - 1,
        top=chain[0].yc + 0.5 * pitch,
        bottom=chain[-1].yc - 0.5 * pitch,
        style=chain[0].style,
    )


def _by_number(
    glyphs: list[Glyph], indexes: list[int], band: _Band, small: float
) -> tuple[dict[int, list[int]], list[int]]:
    """Join each glyph to the line number nearest by height: each numbered line's glyphs, and the rest.

    Sub- and superscripts (smaller than ``_SCRIPT`` x ``small``) sit further
    from their line's centre.
    """
    numbered: dict[int, list[int]] = {}
    rest: list[int] = []
    for i in indexes:
        nearest = min(band.numbers, key=lambda n: abs(n.yc - glyphs[i].yc))
        distance = abs(nearest.yc - glyphs[i].yc)
        if distance <= _NEAR * band.pitch or (
            glyphs[i].size < _SCRIPT * small and distance <= _NEAR_SCRIPT * band.pitch
        ):
            numbered.setdefault(nearest.value, []).append(i)
        else:
            rest.append(i)
    return numbered, rest


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
    """Every ``N:``, ``N.`` or ``N`` token that starts its row (nothing just to its left)."""
    sizes = [g.size for g in glyphs if not g.blank]
    if not sizes:
        return []
    tolerance = _ROW * statistics.median(sizes)
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
            previous = glyphs[tokens[k - 1][-1]] if k > 0 else None
            # "Figure 10:" and the like; a smaller number just right of
            # larger text is algorithm2e's, in the gap beside the text's column.
            if (
                previous is not None
                and first.x0 - previous.x1 < 6
                and previous.size <= _GUTTER_NUMBER_RATIO * first.size
            ):
                continue
            # Only the number's own glyphs ("1." of "1.Sample").
            length, number = 0, []
            for i in token:
                if length >= len(match.group(0)):
                    break
                number.append(i)
                length += len(glyphs[i].text)
            numbers.append(
                _Number(
                    int(match.group(1)),
                    number,
                    first.x0,
                    glyphs[number[-1]].x1,
                    statistics.mean(glyphs[i].yc for i in number),
                    max(glyphs[i].size for i in number),
                    style=match.group(2),
                    row_size=statistics.median(glyphs[i].size for i in row),
                )
            )
    return numbers


def _number_chains(glyphs: list[Glyph]) -> list[list[_Number]]:
    """Runs of line numbers of one style that count up by one, aligned, top to bottom.

    Two numbered rows may be up to ``_MAX_DROP`` rows of text apart (wrapped
    lines, display formulas), measured in the larger of the numbers' and
    their rows' size: algorithm2e sets its numbers smaller than the text.
    """
    chains: list[list[_Number]] = []
    for number in sorted(_line_numbers(glyphs), key=lambda n: -n.yc):
        for chain in chains:
            last = chain[-1]
            drop = last.yc - number.yc
            aligned = abs(number.x1 - last.x1) <= 3 or abs(number.x0 - last.x0) <= 3
            if number.style != ":":  # "1." steps may be indented with a loop (2602.03211 p4)
                aligned = aligned or abs(number.x0 - last.x0) <= _STEP_INDENT * max(last.size, number.size)
            if (
                number.value == last.value + 1
                and number.style == last.style
                and aligned
                and 0 < drop <= _MAX_DROP * max(last.size, number.size, last.row_size, number.row_size)
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
        if g.x0 - right > gap and g.text != COMMENT_MARK:
            break
        right = max(right, g.x1)
    return right


def _fragment(
    page_number: int, glyphs: list[Glyph], band: _Band, used: set[int]
) -> tuple[str | None, AlgorithmFragment] | None:
    """Step 2, band to fragment: column bounds, header rows and lines; None without a keyword."""
    free = [i for i, g in enumerate(glyphs) if i not in used and not g.blank]
    gap = _COLUMN_GAP * band.size
    right, column = _right_edge(glyphs, band, free, gap)
    if column is not None:  # the header rows stay in the algorithm's column too
        free = [i for i in free if glyphs[i].x0 < column]
    header_rows, title = _header_rows(glyphs, band, free, gap)
    if band.style != ":" and title is None:
        return None  # "1." or "1" without a title: an ordinary numbered list
    header_glyphs = {i for row in header_rows for i in row}
    left, top = band.left, band.top
    if header_rows:
        top = max(glyphs[i].yc for i in header_rows[-1]) + _ROW * band.pitch
        left = min(left, min(glyphs[i].x0 for i in header_glyphs) - 1)

    members = {
        i for i in free if band.bottom <= glyphs[i].yc <= top and left <= glyphs[i].xc <= right + 1
    } | header_glyphs
    members |= {i for n in band.numbers for i in n.glyphs}
    lines = _lines(glyphs, members, band)
    # Match keywords on the line texts, which have spaces between words.
    numbered_lines = [line for line in lines if line.number is not None]
    if not _KEYWORD.search("\n".join(line.text for line in lines)) or _code_listing(numbered_lines):
        return None
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
    return title, AlgorithmFragment(page_number, box, lines, members)


def _code_listing(lines: list[AlgorithmLine]) -> bool:
    """Are these lines (the numbered lines; without line numbers, the rows below the title) a code listing?

    Most of their glyphs are set in a code font; or most are in a font
    without a name (SWE-agent's listings drawn in figures) and their letters
    are monospaced: 90% of them as wide as the median letter.
    """
    body = [g for line in lines for g in line.glyphs]
    if 2 * sum(1 for g in body if _CODE_FONT.search(g.font or "")) > len(body):
        return True
    unnamed = [g for g in body if (g.font or "unknown").lower() == "unknown"]
    if 2 * len(unnamed) <= len(body):
        return False
    # Width over size: listings of two sizes side by side are monospaced too.
    widths = [(g.x1 - g.x0) / g.size for g in unnamed if g.text.strip().isalpha() and g.size > 0]
    if len(widths) < _MONO_LETTERS:
        return False
    median = statistics.median(widths)
    even = sum(1 for w in widths if abs(w - median) <= _MONO_TOLERANCE * median)
    return even >= _MONO_SHARE * len(widths)


def _right_edge(
    glyphs: list[Glyph], band: _Band, free: list[int], gap: float
) -> tuple[float, float | None]:
    """How far right the numbered rows run, and the middle of the gap to another column beside them.

    As far as the numbered rows run without crossing a column gap (the gap
    after the line number is indentation, not a column gap), and the comment
    rows (``▷``) and headings between them. A numbered line runs on past
    that edge while its glyphs continue without a column gap, sub- and
    superscripts included (AutoDesign p7 line 4 ends in H'_{t+1} beyond the
    other lines). Nothing reaches into another column (``_other_column``).
    """
    number_glyphs = {i for n in band.numbers for i in n.glyphs}
    numbers_right = max(n.x1 for n in band.numbers)
    inside = [i for i in free if band.bottom <= glyphs[i].yc <= band.top and glyphs[i].xc >= band.left]
    rows = _rows(glyphs, inside, _ROW * band.pitch)
    column = _other_column(glyphs, rows, number_glyphs, numbers_right, band.size)
    if column is not None:
        inside = [i for i in inside if glyphs[i].x0 < column]
        rows = [kept for kept in ([i for i in row if glyphs[i].x0 < column] for row in rows) if kept]
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
        if first.text != COMMENT_MARK and first.x0 > deepest:
            continue  # a row of another column
        right = max(right, _reach(glyphs, body, first.x0, gap))
    small = statistics.median(glyphs[i].size for i in inside) if inside else band.size
    body = [i for i in inside if i not in number_glyphs and glyphs[i].x0 >= numbers_right - 0.5]
    per_number, _ = _by_number(glyphs, body, band, small)
    for line_glyphs in per_number.values():
        line_glyphs.sort(key=lambda i: glyphs[i].x0)
        first = glyphs[line_glyphs[0]]
        if first.x0 <= deepest:
            right = max(right, _reach(glyphs, line_glyphs, first.x0, gap))
    if column is not None and inside:
        # Beside another column, the numbered rows' whole column is the
        # algorithm's: comments after a wide gap ("// Encode x0") included.
        right = max(right, max(glyphs[i].x1 for i in inside))
    return right, column


def _other_column(
    glyphs: list[Glyph], rows: list[list[int]], number_glyphs: set[int], numbers_right: float, size: float
) -> float | None:
    """The middle of the gap to another column beside the numbered rows, or None.

    The column gap is the first x range, right of where the most indented
    numbered line starts, at least ``_MIN_COLUMN_GAP`` x ``size`` wide, that
    no glyph of the rows crosses, with text right of it at the same height.
    A range with a ``▷`` comment right of it in any row is not a column gap.
    """
    starts = []
    for row in rows:
        body = [i for i in row if i not in number_glyphs and glyphs[i].x0 >= numbers_right - 0.5]
        numbered = any(i in number_glyphs for i in row)
        if numbered and body and glyphs[body[0]].text != COMMENT_MARK:
            starts.append(glyphs[body[0]].x0)
    covered = max(starts, default=numbers_right)
    for i in sorted((i for row in rows for i in row), key=lambda i: glyphs[i].x0):
        g = glyphs[i]
        if g.x1 <= covered:
            continue
        if g.x0 - covered >= _MIN_COLUMN_GAP * size:
            right_of = [
                glyphs[next(j for j in row if glyphs[j].x0 >= g.x0)]
                for row in rows
                if glyphs[row[-1]].x0 >= g.x0
            ]
            if all(first.text != COMMENT_MARK for first in right_of):
                return 0.5 * (covered + g.x0)  # the middle of the column gap
        covered = max(covered, g.x1)
    return None


def _header_rows(
    glyphs: list[Glyph], band: _Band, free: list[int], gap: float
) -> tuple[list[list[int]], str | None]:
    """Header rows above line 1, bottom up: title, Require/Ensure, and what lies between; the title."""
    pitch = band.pitch
    above = [
        i
        for i in free
        if band.top < glyphs[i].yc <= band.top + 6 * pitch and glyphs[i].xc >= band.left - 4
    ]
    rows: list[tuple[list[int], bool]] = []  # (row, is a title or Require/Ensure row)
    plain = 0
    edge = band.top
    title = None
    for row in reversed(_rows(glyphs, above, _ROW * pitch)):
        # A header row ends at the first column gap.
        reach = _reach(glyphs, row, glyphs[row[0]].x0, gap)
        row = [i for i in row if glyphs[i].x1 <= reach + 0.5]
        row_top = max(glyphs[i].yc for i in row)
        if row_top - edge > 2.5 * pitch:
            break
        text = _row_text(glyphs, row)
        header = bool(_HEADER.match(text))
        if header:
            plain = 0
            if is_title(text):
                title = text
        else:
            plain += 1
            if plain > _MAX_PLAIN_HEADER_ROWS:
                break
        rows.append((row, header))
        edge = row_top
    while rows and not rows[-1][1]:
        rows.pop()
    return [row for row, _ in rows], title


def _lines(glyphs: list[Glyph], members: set[int], band: _Band) -> list[AlgorithmLine]:
    """Join each line number with the glyphs at its height; the rest form unnumbered rows."""
    number_glyphs = {i for n in band.numbers for i in n.glyphs}
    small = statistics.median(glyphs[i].size for i in members)
    numbered, rest = _by_number(glyphs, [i for i in members if i not in number_glyphs], band, small)
    placed = [
        (n.yc, _line(glyphs, n.value, n.x0, numbered.get(n.value, []), n.yc)) for n in band.numbers
    ]
    for row in _rows(glyphs, rest, _ROW * band.pitch):
        yc = statistics.mean(glyphs[i].yc for i in row)
        placed.append((yc, _line(glyphs, None, None, row, yc)))
    placed.sort(key=lambda item: -item[0])
    return [line for _, line in placed]


def _line(glyphs: list[Glyph], number, number_x, body: list[int], yc: float) -> AlgorithmLine:
    body = sorted(body, key=lambda i: glyphs[i].x0)
    text_glyphs = [i for i in body if not glyphs[i].blank]
    line_glyphs = [glyphs[i] for i in text_glyphs]
    return AlgorithmLine(
        number=number,
        text=_row_text(glyphs, body),
        x=glyphs[text_glyphs[0]].x0 if text_glyphs else (number_x or 0.0),
        y=statistics.median(glyphs[i].y0 for i in text_glyphs) if text_glyphs else yc,
        number_x=number_x,
        runs=_runs(glyphs, body),
        glyphs=line_glyphs,
        indexes=text_glyphs,
        keep=keep_flags(line_glyphs),
    )


def keep_flags(line: list[Glyph]) -> list[bool]:
    """For each glyph of a line (left to right, no blanks): does it keep its source text?"""
    keep = [bool(_MATH_FONT.search(g.font or "") or _CODE_FONT.search(g.font or "")) for g in line]
    words = _words(line)
    # A row set all in bold with a word that is not a keyword is a heading
    # ("Asset generation and selection"): its words are not keywords.
    texts = ["".join(line[k].text for k in word).rstrip(":").lower() for word in words]
    heading = all(g.bold for g, math in zip(line, keep) if not math) and any(
        t not in KEYWORDS and any(c.isalpha() for c in t) and not keep[word[0]]
        for t, word in zip(texts, words)
    )
    for word in words:
        glyphs = [line[k] for k in word]
        text = "".join(g.text for g in glyphs)
        if not heading and all(g.bold for g in glyphs) and text.rstrip(":").lower() in KEYWORDS:
            for k in word:
                keep[k] = True
    # "to" between two kept runs: for i = 0 to 25 do.
    for word in words:
        text = "".join(line[k].text for k in word)
        if (
            text in _RANGE_WORDS
            and word[0] > 0
            and word[-1] + 1 < len(line)
            and keep[word[0] - 1]
            and keep[word[-1] + 1]
        ):
            for k in word:
                keep[k] = True
    # Names: constants and identifiers (ticket 16), and function calls (a
    # name and everything inside its parentheses, D5).
    tokens = _tokens(line)
    # A row whose letters are all capitals is a heading in capitals: its
    # words are no constants. So are the words of a bold heading.
    constants = not heading and any(g.text.islower() for g in line)
    k = 0
    while k < len(line):
        name = _name(line, words, tokens, k, constants)
        if name is None:
            k += 1
            continue
        depth = 0
        end = name[-1] + 1
        for j in name:
            keep[j] = True
            depth += line[j].text.count("(") - line[j].text.count(")")
        if depth == 0 and end < len(line) and line[end].text == "(":
            depth, keep[end] = 1, True
            end += 1
        while depth > 0 and end < len(line):
            keep[end] = True
            depth += line[end].text.count("(") - line[end].text.count(")")
            end += 1
        k = end
    # Set literals: everything between braces set in a math font ({left,right}).
    depth = 0
    for k, g in enumerate(line):
        if depth > 0:
            keep[k] = True
        if keep[k] and g.text == "{":
            depth += 1
        elif keep[k] and g.text == "}" and depth > 0:
            depth -= 1
    # The title's "Algorithm N" and the Require/Ensure/Input/Output labels.
    label = _LABEL.match("".join(g.text for g in line))
    if label:
        for k in range(len(label.group(0))):
            keep[k] = True
    return keep


def word_gap(left: Glyph, right: Glyph) -> bool:
    """Is there a word gap between two glyphs of a row, ``left`` before ``right``?"""
    return right.x0 - left.x1 > _WORD_GAP * right.size


def is_title(text: str) -> bool:
    """Is a row with this text an algorithm's title (``Algorithm N``, ``Procedure``)?"""
    return bool(_TITLE.match(text))


def _words(line: list[Glyph]) -> list[list[int]]:
    """Positions of the line's glyphs grouped into words: a word gap or a font change ends a word."""
    words: list[list[int]] = []
    for k, g in enumerate(line):
        if words:
            last = line[words[-1][-1]]
            if not word_gap(last, g) and g.font == last.font:
                words[-1].append(k)
                continue
        words.append([k])
    return words


def _row_text(glyphs: list[Glyph], row: list[int]) -> str:
    """The row's text, with a space where glyphs are a word gap apart."""
    parts = []
    previous = None
    for i in sorted(row, key=lambda i: glyphs[i].x0):
        g = glyphs[i]
        if g.blank:
            continue
        if previous is not None and word_gap(previous, g):
            parts.append(" ")
        parts.append(g.text)
        previous = g
    return "".join(parts)


def _runs(glyphs: list[Glyph], body: list[int]) -> list[dict]:
    """Glyph runs of one line: same font, size, bold and small caps."""
    chars = [glyphs[i] for i in sorted(body, key=lambda i: glyphs[i].x0) if not glyphs[i].blank]
    runs: list[dict] = []
    for positions in _words(chars):
        word = [chars[k] for k in positions]
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


def _tokens(line: list[Glyph]) -> list[list[int]]:
    """Positions of the line's glyphs grouped at word gaps only (across font changes)."""
    tokens: list[list[int]] = []
    for k, g in enumerate(line):
        if tokens and not word_gap(line[tokens[-1][-1]], g):
            tokens[-1].append(k)
        else:
            tokens.append([k])
    return tokens


def _name(
    line: list[Glyph], words: list[list[int]], tokens: list[list[int]], k: int, constants: bool
) -> list[int] | None:
    """The positions of the constant or identifier that starts at position ``k``, if one does."""
    token = next((t for t in tokens if t[0] == k), None)
    if token is not None and _identifier([line[j] for j in token]):
        return token
    word = next((w for w in words if w[0] == k), None)
    if word is None:
        return None
    glyphs = [line[j] for j in word]
    if _small_caps(glyphs) or _SMALL_CAPS_FONT.search(glyphs[0].font or ""):
        return word
    letters = [g.text for g in glyphs if g.text.isalpha()]
    if constants and len(letters) >= _CONSTANT_LETTERS and all(c.isupper() for c in letters):
        return word
    return None


def _identifier(token: list[Glyph]) -> bool:
    """An underscore between letters or digits (max_retry), or camelCase in one font (SelectParents)."""
    text = "".join(g.text for g in token)
    return bool(re.search(r"[^\W_]_+[^\W_]", text)) or any(
        a.text.islower() and b.text.isupper() and a.font == b.font for a, b in zip(token, token[1:])
    )
