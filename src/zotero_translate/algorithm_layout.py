"""Line-by-line layout of translated algorithms, M2 ticket 02.

The engine calls ``AlgorithmLayout`` after it has typeset all pages (see
``TranslationConfig.algorithm_typesetter``). The algorithm's characters are
still the source characters in ``page.pdf_character`` (ticket 01). For each
row with natural language this replaces the row's characters:

- The row starts at the source row's start x, on the source row's
  baseline; the line number is not touched (its source glyphs stay).
- Placeholders are the source characters themselves, moved sideways only,
  so small caps, sub- and superscripts and math glyphs stay as they were.
  So is every English word of the translation that the source row has as a
  word of its own (names, small caps or typewriter words the model kept).
  A placeholder that a glyph outside the algorithm touches (a formula cut
  by the algorithm's edge) is not whole: the algorithm keeps its source.
- A drawn line of the row (a radical's or fraction's bar, an identifier's
  underscore; ``AlgorithmLine.drawn``, closure ticket 14) moves with the
  glyphs it belongs to, by the same dx (``_drawn_line_shifts``); if those
  glyphs moved by different amounts, the algorithm keeps its source.
- Translated text gets the style of the row's first natural-language glyph
  (font family, size, bold, colour), not the style of the algorithm's first
  glyph: a bold keyword no longer makes the whole row bold.
- A ``▷`` comment stays a part of its own, right-aligned to where the source
  comment ends.
- A row that does not fit shrinks its translated text, down to
  ``MIN_SCALE`` of the source size; one that still does not fit makes the
  whole algorithm keep its source text, as does a row whose start moves more
  than ``X_TOLERANCE``. Rows without natural language keep their source
  characters as they are.

An algorithm on several pages is laid out on all of them before any page is
changed, so it is either translated everywhere or nowhere.
"""

from __future__ import annotations

import copy
import re
import unicodedata

from zotero_translate.algorithm_translation import PLACEHOLDER
from zotero_translate.algorithm_translation import AlgorithmTranslator
from zotero_translate.algorithm_translation import Part
from zotero_translate.algorithm_translation import where
from zotero_translate.algorithms import AlgorithmDetector
from zotero_translate.algorithms import AlgorithmUnit

# Smallest translated text, as a fraction of the source row's size
# (10 pt -> 8 pt). Determined on MaLiang-Harness (M2 ticket 02).
MIN_SCALE = 0.8
# How far a row's start may move from the source row's start, in pt.
X_TOLERANCE = 1.0
_SCALE_STEP = 0.05
_SPACE = 0.25  # width of a space, as a fraction of the font size
_JOIN = 0.15  # gap between text and a placeholder written without a space (none at punctuation)
_RIGHT_SLACK = 0.5  # pt a row may run past the algorithm's right edge
_WORD = r"[A-Za-z0-9](?:[A-Za-z0-9'’_.\-]*[A-Za-z0-9])?"


class LayoutError(Exception):
    """A row does not fit; the message is a readable Chinese reason."""


class AlgorithmLayout:
    def __init__(self, detector: AlgorithmDetector, translator: AlgorithmTranslator):
        self.detector = detector
        self.translator = translator

    def __call__(self, document, new_char) -> None:
        for unit in self.detector.units:
            result = self.translator.result(unit)
            if result is None:
                continue
            parts, texts = result
            try:
                changes = [
                    _layout_fragment(document.page[fragment.page - 1], unit, f, parts, texts, new_char)
                    for f, fragment in enumerate(unit.fragments)
                ]
            except LayoutError as e:
                self.translator.keep_source(unit, f"排版失败：{e}")
                continue
            except Exception as e:  # noqa: BLE001 - only this algorithm keeps its source
                self.translator.keep_source(unit, f"排版出错：{type(e).__name__}: {e}")
                continue
            for page, removed, added, shifts in changes:
                page.pdf_character = [c for c in page.pdf_character if id(c) not in removed] + added
                for curve, dx in shifts:
                    _shift_curve(curve, dx)
            unit.mark_translated()


def _layout_fragment(page, unit: AlgorithmUnit, f: int, parts: list[Part], texts: dict[int, str], new_char):
    fragment = unit.fragments[f]
    on_page = {id(c) for c in page.pdf_character}
    if any(id(c) not in on_page for c in fragment.chars):
        raise LayoutError(f"第 {fragment.page} 页上的算法原文字符已变化")
    members = {id(c) for c in fragment.chars}
    others = [
        c
        for c in page.pdf_character
        if id(c) not in members and c.box is not None and (c.char_unicode or "").strip()
    ]
    right_edge = fragment.box[2] + _RIGHT_SLACK
    removed: set[int] = set()
    added: list = []
    shifts: list = []  # (engine curve, dx): drawn lines that move with their glyphs
    on_page_curves = {id(c) for c in page.pdf_curve or []}
    rows: dict[int, list[Part]] = {}
    for part in parts:
        if part.fragment == f:
            rows.setdefault(id(part.line), []).append(part)
    for row in rows.values():
        for part in row:
            if part.has_text:
                _check_complete(part, others)
        body = next((p for p in row if p.kind != "comment"), None)
        comment = next((p for p in row if p.kind == "comment"), None)
        line = row[0].line
        limit = right_edge
        moves: dict[int, float] = {}  # position of a source glyph placed again -> its dx
        if comment is not None:
            # Right-aligned at the source comment's end; never over the body.
            body_end = (
                max(line.glyphs[k].x1 for k in body.positions) if body is not None else fragment.box[0]
            )
            if comment.has_text:
                chars, left, moved = _place_comment(page, comment, texts[comment.id], body_end, new_char)
                added += chars
                moves |= moved
                removed |= {id(line.chars[k]) for k in [comment.mark, *comment.positions]}
            else:
                left = line.glyphs[comment.mark].x0
            limit = left - 0.5 * line.glyphs[comment.mark].size
        if body is not None and body.has_text:
            start = line.glyphs[body.positions[0]].x0
            chars, _, moved = _fit(page, body, texts[body.id], start, limit, new_char)
            first = min(c.box.x for c in chars)
            if abs(first - start) > X_TOLERANCE:
                raise LayoutError(f"{where(body)}的起始横坐标偏离原文 {abs(first - start):.1f} pt")
            added += chars
            moves |= moved
            removed |= {id(line.chars[k]) for k in body.positions}
        shifts += _drawn_line_shifts(row[0], moves, on_page_curves)
    return page, removed, added, shifts


def _drawn_line_shifts(part: Part, moves: dict[int, float], on_page: set[int]) -> list:
    """Each drawn line of the row moves with the glyphs it belongs to (closure ticket 14).

    Its glyphs are kept ones, so they all move by one dx (one placeholder) or
    stay (a part without text). Glyphs moved by different amounts, or a line
    the page no longer has, make the algorithm keep its source text.
    """
    line = part.line
    shifts = []
    for (_, positions), curve in zip(line.drawn, line.curves):
        dxs = {round(moves[k], 3) for k in positions if k in moves}
        if not dxs:
            continue
        if len(dxs) > 1 or any(k not in moves for k in positions):
            raise LayoutError(f"{where(part)}中画出的线所属的字符被拆开")
        if id(curve) not in on_page:
            raise LayoutError(f"{where(part)}中画出的线不在页面上")
        shifts.append((curve, dxs.pop()))
    return shifts


def _place_comment(page, part: Part, text: str, left_limit: float, new_char):
    """The comment with its ▷ sign, ending where the source comment ends."""
    line = part.line
    mark = line.glyphs[part.mark]
    end = max(line.glyphs[k].x1 for k in part.positions)
    gap = line.glyphs[part.positions[0]].x0 - mark.x1
    scale = 1.0
    while True:
        chars, width, moves = _row(page, part, text, 0.0, scale, new_char)
        left = end - width - gap - (mark.x1 - mark.x0)
        if left >= left_limit + 0.5 * mark.size:
            break
        scale = round(scale - _SCALE_STEP, 2)
        if scale < MIN_SCALE - 1e-9:
            raise LayoutError(f"{where(part)}放不下（字号缩到原文的 {MIN_SCALE:.0%} 仍超出）")
    dx = left + (mark.x1 - mark.x0) + gap
    for c in chars:
        _shift(c, dx)
    sign = copy.deepcopy(line.chars[part.mark])
    _shift(sign, left - sign.box.x)
    return [sign, *chars], left, {k: v + dx for k, v in moves.items()}


def _fit(page, part: Part, text: str, start: float, limit: float, new_char):
    scale = 1.0
    while True:
        chars, width, moves = _row(page, part, text, start, scale, new_char)
        if start + width <= limit:
            return chars, width, moves
        scale = round(scale - _SCALE_STEP, 2)
        if scale < MIN_SCALE - 1e-9:
            raise LayoutError(f"{where(part)}放不下（字号缩到原文的 {MIN_SCALE:.0%} 仍超出）")


def _row(page, part: Part, text: str, start: float, scale: float, new_char):
    """The row's characters from x = ``start``, translated text at ``scale``; its width; and
    the dx of every source glyph placed again (position -> dx)."""
    line = part.line
    style = next(k for k in part.positions if not line.keep[k]) if part.has_text else part.positions[0]
    like = line.chars[style]
    size = line.glyphs[style].size
    cursor = start
    previous = None  # "text", "keep" or "space"
    last = ""  # the last character of translated text
    chars = []
    moves: dict[int, float] = {}
    words = _source_words(part)
    for token in re.split(r"(\{v\d+\}|" + _WORD + ")", text):
        if not token:
            continue
        reused = None
        if re.fullmatch(_WORD, token) and words.get(token):
            # An English word of the source row (a name, a small caps or
            # typewriter word the model kept): its source glyphs, not new ones.
            reused = words[token].pop(0)
        if PLACEHOLDER.fullmatch(token) or reused is not None:
            positions = reused if reused is not None else part.keeps[token]
            if previous == "text" and not _punctuation(last):
                cursor += _JOIN * size
            source = [line.chars[k] for k in positions]
            dx = cursor - min(c.box.x for c in source)
            moves |= {k: dx for k in positions}
            for c in source:
                moved = copy.deepcopy(c)
                _shift(moved, dx)
                chars.append(moved)
            cursor = max(c.box.x2 for c in chars[-len(source) :])
            previous = "keep"
            continue
        for ch in token:
            if ch.isspace():
                if previous != "space":
                    cursor += _SPACE * size * scale
                previous = "space"
                continue
            if previous == "keep" and not _punctuation(ch):
                cursor += _JOIN * size
            char = new_char(page, like, ch, size * scale)
            if char is None:
                raise LayoutError(f"{where(part)}中的字符“{ch}”没有可用字形")
            width = char.box.x2 - char.box.x
            char.box.x, char.box.x2 = cursor, cursor + width
            chars.append(char)
            cursor += width
            previous, last = "text", ch
    return chars, cursor - start, moves


def _check_complete(part: Part, others: list) -> None:
    """A kept fragment that moves must be whole: no glyph outside the algorithm touches it."""
    for name, positions in part.keeps.items():
        run = [part.line.chars[k] for k in positions]
        x0, x1 = min(c.box.x for c in run), max(c.box.x2 for c in run)
        y0, y1 = min(c.box.y for c in run), max(c.box.y2 for c in run)
        size = max(c.pdf_style.font_size for c in run)
        for c in others:
            if c.box.y2 > y0 and c.box.y < y1 and (
                0 <= c.box.x - x1 < 0.3 * size or 0 <= x0 - c.box.x2 < 0.3 * size
            ):
                raise LayoutError(
                    f"{where(part)}的保留片段 {name} 不完整：相邻的“{c.char_unicode}”不在算法区域内"
                )


def _source_words(part: Part) -> dict[str, list[list[int]]]:
    """The row's natural-language words (no punctuation at their ends) -> their glyph positions."""
    line = part.line
    words: dict[str, list[list[int]]] = {}
    current: list[int] = []
    text_positions = [k for k in part.positions if not line.keep[k]]
    for i, k in enumerate(text_positions + [None]):
        if k is not None and current and k == current[-1] + 1 and not _gap(line, current[-1], k):
            current.append(k)
            continue
        while current and not line.glyphs[current[0]].text.isalnum():
            current = current[1:]
        while current and not line.glyphs[current[-1]].text.isalnum():
            current = current[:-1]
        if current:
            words.setdefault("".join(line.glyphs[j].text for j in current), []).append(current)
        current = [k] if k is not None else []
    return words


def _gap(line, a: int, b: int) -> bool:
    return line.glyphs[b].x0 - line.glyphs[a].x1 > 0.2 * line.glyphs[b].size


def _punctuation(ch: str) -> bool:
    return bool(ch) and unicodedata.category(ch).startswith("P")


def _shift(char, dx: float) -> None:
    char.box.x += dx
    char.box.x2 += dx
    if char.visual_bbox is not None and char.visual_bbox.box is not None:
        char.visual_bbox.box.x += dx
        char.visual_bbox.box.x2 += dx


def _shift_curve(curve, dx: float) -> None:
    """Move a drawn line sideways: a translation in page space before its own transforms."""
    curve.box.x += dx
    curve.box.x2 += dx
    matrix = list(curve.relocation_transform or [1, 0, 0, 1, 0, 0])
    if len(matrix) != 6:
        matrix = [1, 0, 0, 1, 0, 0]
    matrix[4] = float(matrix[4]) + dx
    curve.relocation_transform = matrix


