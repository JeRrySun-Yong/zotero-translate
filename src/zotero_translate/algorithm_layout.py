"""Line-by-line layout of translated algorithms, M2 ticket 02.

The engine calls ``AlgorithmLayout`` after it has typeset all pages (see
``TranslationConfig.algorithm_typesetter``). The algorithm's characters are
still the source characters in ``page.pdf_character`` (ticket 01). For each
row with natural language this replaces the row's characters:

- The row starts at the source row's start x, on the source row's
  baseline; the line number is not touched (its source glyphs stay).
- Placeholders are the source characters themselves, moved sideways only,
  so small caps, sub- and superscripts and math glyphs stay as they were.
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
from zotero_translate.algorithms import TRANSLATED
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
            for page, removed, added in changes:
                page.pdf_character = [c for c in page.pdf_character if id(c) not in removed] + added
            unit.result = TRANSLATED
            unit.reason = None


def _layout_fragment(page, unit: AlgorithmUnit, f: int, parts: list[Part], texts: dict[int, str], new_char):
    fragment = unit.fragments[f]
    on_page = {id(c) for c in page.pdf_character}
    if any(id(c) not in on_page for c in fragment.chars):
        raise LayoutError(f"第 {fragment.page} 页上的算法原文字符已变化")
    right_edge = fragment.box[2] + _RIGHT_SLACK
    removed: set[int] = set()
    added: list = []
    rows: dict[int, list[Part]] = {}
    for part in parts:
        if part.fragment == f:
            rows.setdefault(id(part.line), []).append(part)
    for row in rows.values():
        body = next((p for p in row if p.kind != "comment"), None)
        comment = next((p for p in row if p.kind == "comment"), None)
        line = row[0].line
        limit = right_edge
        if comment is not None:
            # Right-aligned at the source comment's end; never over the body.
            body_end = (
                max(line.glyphs[k].x1 for k in body.positions) if body is not None else fragment.box[0]
            )
            if comment.has_text:
                chars, left = _place_comment(page, comment, texts[comment.id], body_end, new_char)
                added += chars
                removed |= {id(line.chars[k]) for k in [comment.mark, *comment.positions]}
            else:
                left = line.glyphs[comment.mark].x0
            limit = left - 0.5 * line.glyphs[comment.mark].size
        if body is not None and body.has_text:
            start = line.glyphs[body.positions[0]].x0
            chars, _ = _fit(page, body, texts[body.id], start, limit, new_char)
            first = min(c.box.x for c in chars)
            if abs(first - start) > X_TOLERANCE:
                raise LayoutError(f"{where(body)}的起始横坐标偏离原文 {abs(first - start):.1f} pt")
            added += chars
            removed |= {id(line.chars[k]) for k in body.positions}
    return page, removed, added


def _place_comment(page, part: Part, text: str, left_limit: float, new_char):
    """The comment with its ▷ sign, ending where the source comment ends."""
    line = part.line
    mark = line.glyphs[part.mark]
    end = max(line.glyphs[k].x1 for k in part.positions)
    gap = line.glyphs[part.positions[0]].x0 - mark.x1
    scale = 1.0
    while True:
        chars, width = _row(page, part, text, 0.0, scale, new_char)
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
    return [sign, *chars], left


def _fit(page, part: Part, text: str, start: float, limit: float, new_char):
    scale = 1.0
    while True:
        chars, width = _row(page, part, text, start, scale, new_char)
        if start + width <= limit:
            return chars, width
        scale = round(scale - _SCALE_STEP, 2)
        if scale < MIN_SCALE - 1e-9:
            raise LayoutError(f"{where(part)}放不下（字号缩到原文的 {MIN_SCALE:.0%} 仍超出）")


def _row(page, part: Part, text: str, start: float, scale: float, new_char):
    """The row's characters from x = ``start``, translated text at ``scale``; and its width."""
    line = part.line
    style = next(k for k in part.positions if not line.keep[k]) if part.has_text else part.positions[0]
    like = line.chars[style]
    size = line.glyphs[style].size
    cursor = start
    previous = None  # "text", "keep" or "space"
    last = ""  # the last character of translated text
    chars = []
    for token in re.split(r"(\{v\d+\})", text):
        if not token:
            continue
        if PLACEHOLDER.fullmatch(token):
            positions = part.keeps[token]
            if previous == "text" and not _punctuation(last):
                cursor += _JOIN * size
            source = [line.chars[k] for k in positions]
            dx = cursor - min(c.box.x for c in source)
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
    return chars, cursor - start


def _punctuation(ch: str) -> bool:
    return bool(ch) and unicodedata.category(ch).startswith("P")


def _shift(char, dx: float) -> None:
    char.box.x += dx
    char.box.x2 += dx
    if char.visual_bbox is not None and char.visual_bbox.box is not None:
        char.visual_bbox.box.x += dx
        char.visual_bbox.box.x2 += dx


