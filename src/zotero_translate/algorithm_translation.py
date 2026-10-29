"""Line-by-line translation of detected algorithms (pseudocode), M2 ticket 02.

Each algorithm unit gets one translation request of its own. Every row of
the algorithm is an input item: the title, each numbered line, each row
without a number (``Require:``, headings) and each ``▷`` comment (a part of
its own, so that it can stay right-aligned). In an item's source, every run
of glyphs that keeps its source text (``AlgorithmLine.keep``: formulas,
bold keywords, function calls with their arguments, the ``Algorithm N``
label) is a placeholder ``{vN}``; the item also lists what each placeholder
stands for. The request carries the condensed paper dossier, the glossary
entries found in the algorithm, the title of the section the algorithm is
in and the whole algorithm's source text; no first-mention glosses.

The answer must keep the structure (``structure_problems``): as many items
as rows, the same ids and line numbers in the same order, and in each item
exactly the placeholders of that row. An answer that breaks it is resent
like an unparsable one, within the pipeline model client's retries (ADR
0002). Then one review request, as in M1, and the mechanical checks
(``checks.problems``) on each row: the reviewed row if it passes, else the
draft if it passes. A review answer that breaks the structure until the
retries are used up leaves the drafts to be judged alone; a review request
that gets no answer at all makes the algorithm keep its source text.

Whatever fails (the request, the structure, the checks, and later the
layout in ``algorithm_layout``) makes only this algorithm keep its source
text, with the reason in the task report; the paper task goes on.
"""

from __future__ import annotations

import itertools
import json
import re
import threading
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import field

from zotero_translate.algorithms import COMMENT_MARK
from zotero_translate.algorithms import KEYWORD_PHRASES
from zotero_translate.algorithms import AlgorithmDetector
from zotero_translate.algorithms import AlgorithmLine
from zotero_translate.algorithms import AlgorithmUnit
from zotero_translate.algorithms import is_title
from zotero_translate.algorithms import word_gap
from zotero_translate.batch_context import condense
from zotero_translate.checks import problems
from zotero_translate.model_client import ModelCallError
from zotero_translate.model_client import ModelClient

PLACEHOLDER = re.compile(r"\{v\d+\}")

ALGORITHM_MARKER = "Translate the natural language in the rows of a pseudocode algorithm"
ALGORITHM_REVIEW_MARKER = "Review the draft translation of the rows of a pseudocode algorithm"

_KEEP_RULES = f"""\
## Keep exactly as in the source
- Placeholders {{v1}}, {{v2}}, ...: each item's output has exactly the placeholders of its own source, each once, never one from another item. "keep" shows what each stands for: keywords, identifiers, function calls with their arguments, formulas, the algorithm number. Write the placeholder, never its content.
- Keywords stay in English, never translated: {", ".join(KEYWORD_PHRASES)}. Most of them are inside placeholders; one written as plain text stays as it is.
- Function calls: a function name and everything inside its parentheses are identifiers and stay in English, even words such as FILLBEZIERSHAPE(C, hair silhouette, brown gradient).
- Numbers, variable names, code identifiers, model names: character for character.
- Terminology: follow the glossary. A target term identical to its source term is a key concept of this paper: keep it in English exactly as written. No explanations or glosses in parentheses.
"""

_OUTPUT_FORMAT = """\
## Output format
A JSON array with one object per input item, in the input order: {"id": <the item's id>, "line": <the item's line>, "output": "<translation>"}. No other fields, no text before or after, no ```json fences.
"""

INSTRUCTIONS = (
    f"""\
{ALGORITHM_MARKER} from an English academic paper into Simplified Chinese.

Each input item is one row of the algorithm: the title ("part": "title"), a numbered line ("line" is its line number), a row without a number ("line": null), or a comment after the "▷" sign ("part": "comment"; the sign itself is not in the text). Translate each item on its own: never move text between items, merge items or split them.

## What to translate
The natural language: step descriptions, comments, headings, the description in the title. Keep it concise: each translated row must fit on one line like the source row. An item with no natural language is returned unchanged.

"""
    + _KEEP_RULES
    + "\n"
    + _OUTPUT_FORMAT
)

REVIEW_INSTRUCTIONS = (
    f"""\
{ALGORITHM_REVIEW_MARKER} from an English academic paper into Simplified Chinese, and return the revised translation.

Each input item is one row of the algorithm with its source and its draft. Fix mistranslations, omissions, additions and unidiomatic Chinese; keep each row concise enough to fit on one line. If a draft is already right, return it unchanged. Never move text between items, merge items or split them.

"""
    + _KEEP_RULES
    + "\n"
    + _OUTPUT_FORMAT
)


@dataclass
class Part:
    """One input item: a row of an algorithm, or the ``▷`` comment at the end of a row."""

    id: int
    fragment: int  # index of the unit's fragment
    line: AlgorithmLine
    kind: str  # "title", "line" (numbered or not) or "comment"
    positions: list[int]  # positions in ``line.glyphs`` (a comment's ▷ sign not included)
    source: str  # with placeholders
    keeps: dict[str, list[int]] = field(default_factory=dict)  # placeholder -> positions
    has_text: bool = False  # has natural language to translate
    mark: int | None = None  # position of a comment's ▷ sign

    @property
    def number(self) -> int | None:
        return self.line.number

    def item(self) -> dict:
        keep = {
            name: "".join(_spaced(self.line, positions)) for name, positions in self.keeps.items()
        }
        return {"id": self.id, "line": self.number, "part": self.kind, "source": self.source, "keep": keep}


def parts_of(unit: AlgorithmUnit) -> list[Part]:
    """The unit's input items, top to bottom; placeholders are numbered across the unit."""
    counter = itertools.count(1)
    parts: list[Part] = []
    for f, fragment in enumerate(unit.fragments):
        for line in fragment.lines:
            if not line.glyphs:
                continue
            marks = [k for k, g in enumerate(line.glyphs) if g.text == COMMENT_MARK]
            body = list(range(marks[0] if marks else len(line.glyphs)))
            kind = "title" if line.number is None and is_title(line.text) else "line"
            if body:
                parts.append(_part(len(parts), f, line, kind, body, counter))
            if marks:
                comment = list(range(marks[0] + 1, len(line.glyphs)))
                if comment:
                    part = _part(len(parts), f, line, "comment", comment, counter)
                    part.mark = marks[0]
                    parts.append(part)
    return parts


def _part(id: int, fragment: int, line: AlgorithmLine, kind: str, positions: list[int], counter) -> Part:
    pieces: list[str] = []
    keeps: dict[str, list[int]] = {}
    has_text = False
    previous = None
    for keep, run in itertools.groupby(positions, key=lambda k: line.keep[k]):
        run = list(run)
        if previous is not None and _gap(line, previous, run[0]):
            pieces.append(" ")
        if keep:
            name = f"{{v{next(counter)}}}"
            keeps[name] = run
            pieces.append(name)
        else:
            text = "".join(_spaced(line, run))
            # A word of two letters or more; labels such as "b,c" are not text.
            has_text = has_text or bool(re.search(r"[^\W\d_]{2,}", text))
            pieces.append(text)
        previous = run[-1]
    return Part(id, fragment, line, kind, positions, "".join(pieces), keeps, has_text)


def _gap(line: AlgorithmLine, a: int, b: int) -> bool:
    """Is there a word gap between the glyphs at positions ``a`` and ``b``?"""
    return word_gap(line.glyphs[a], line.glyphs[b])


def _spaced(line: AlgorithmLine, positions: list[int]) -> list[str]:
    out = []
    for i, k in enumerate(positions):
        if i and _gap(line, positions[i - 1], k):
            out.append(" ")
        out.append(line.glyphs[k].text)
    return out


def structure_problems(parts: list[Part], content: str) -> tuple[dict[int, str], list[str]]:
    """The outputs of an answer by item id, and how the answer breaks the algorithm's structure."""
    outputs = _parse(content)
    found = []
    if len(outputs) != len(parts):
        found.append(f"行数不一致：原文 {len(parts)} 行，译文 {len(outputs)} 行")
    ids = [o.get("id") for o in outputs]
    if ids != [p.id for p in parts]:
        found.append("行的编号或顺序与原文不一致")
    lines = [o.get("line") for o in outputs]
    if lines != [p.number for p in parts][: len(lines)] or len(lines) != len(parts):
        found.append("行号序列与原文不一致")
    by_id = {o.get("id"): o.get("output") for o in outputs}
    for part in parts:
        output = by_id.get(part.id)
        if not isinstance(output, str):
            found.append(f"第 {part.id} 项缺少译文")
            continue
        if sorted(PLACEHOLDER.findall(output)) != sorted(part.keeps):
            found.append(
                f"{where(part)}的占位符与原文同一行不同：原文 {' '.join(part.keeps) or '无'}，"
                f"译文 {' '.join(PLACEHOLDER.findall(output)) or '无'}"
            )
    return {k: v for k, v in by_id.items() if isinstance(v, str)}, found


def where(part: Part) -> str:
    """The row in readable Chinese, for reasons in the task report."""
    if part.number is not None:
        return f"第 {part.number} 行" + ("的注释" if part.kind == "comment" else "")
    return {"title": "标题", "comment": "注释行"}.get(part.kind, "无行号的行")


def _parse(content: str) -> list[dict]:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    value = json.loads(text)
    if not isinstance(value, list) or not all(isinstance(o, dict) for o in value):
        raise ValueError("译文不是 JSON 对象数组")
    return value


class AlgorithmTranslator:
    """Translates every detected algorithm; the engine calls ``start`` (see TranslationConfig)."""

    def __init__(
        self,
        client: ModelClient,
        dossier: dict,
        detector: AlgorithmDetector,
        glossary_entries: list[tuple[str, str]],
    ):
        self.client = client
        self.condensed_dossier = condense(dossier)
        self.detector = detector
        self.glossary_entries = glossary_entries
        self.parts: dict[int, list[Part]] = {}  # id(unit) -> parts
        self.results: dict[int, Future] = {}  # id(unit) -> {part id: final text} or None
        self._lock = threading.Lock()

    def start(self, paragraphs: list[tuple[int, object, str | None]]) -> None:
        """Send every algorithm's request in the background.

        ``paragraphs`` are (0-based page, box, section title) in reading order.
        """
        units = self.detector.units
        if not units:
            return
        pool = ThreadPoolExecutor(max_workers=len(units), thread_name_prefix="algorithm")
        for unit in units:
            parts = parts_of(unit)
            self.parts[id(unit)] = parts
            self.results[id(unit)] = pool.submit(
                self._translate, unit, parts, section_of(unit, paragraphs)
            )
        pool.shutdown(wait=False)

    def result(self, unit: AlgorithmUnit) -> tuple[list[Part], dict[int, str]] | None:
        """The unit's parts and final text by part id; None if it keeps its source text."""
        future = self.results.get(id(unit))
        if future is None:
            return None
        try:
            texts = future.result()
        except Exception as e:  # noqa: BLE001 - a bug here must not fail the paper task
            self.keep_source(unit, f"算法翻译出错：{type(e).__name__}: {e}")
            return None
        if texts is None:
            return None
        return self.parts[id(unit)], texts

    def keep_source(self, unit: AlgorithmUnit, reason: str) -> None:
        with self._lock:
            unit.keep_source(reason)

    def _translate(self, unit: AlgorithmUnit, parts: list[Part], section: str | None) -> dict[int, str] | None:
        if not any(part.has_text for part in parts):
            return {part.id: part.source for part in parts}
        entries = self._entries(unit)
        translated_terms = frozenset(s.lower() for s, t in entries if s != t)
        items = [part.item() for part in parts]
        prompt = self._prompt(INSTRUCTIONS, unit, section, entries, items)
        page = unit.fragments[0].page

        def validate(content: str) -> None:
            _, found = structure_problems(parts, content)
            if found:
                raise ValueError("结构校验失败：" + "；".join(found[:3]))

        try:
            content = self.client.complete(
                [{"role": "user", "content": prompt}], validate=validate, page=page
            )
        except ModelCallError as e:
            reason = str(e) if e.answered else f"请求失败：{e}"
            self.keep_source(unit, f"算法翻译{reason}")
            return None
        drafts, found = structure_problems(parts, content)
        if found:  # a cached answer is judged like a fresh one
            self.keep_source(unit, "结构校验失败：" + "；".join(found[:3]))
            return None

        review_items = [dict(item, draft=drafts[item["id"]]) for item in items]
        review_prompt = self._prompt(REVIEW_INSTRUCTIONS, unit, section, entries, review_items)
        try:
            reviewed_content = self.client.complete(
                [{"role": "user", "content": review_prompt}], validate=validate, page=page
            )
            reviewed, found = structure_problems(parts, reviewed_content)
            if found:
                reviewed = {}
        except ModelCallError as e:
            if not e.answered:  # no answer at all: like the translation request
                self.keep_source(unit, f"算法审校请求失败：{e}")
                return None
            reviewed = {}  # no usable answer: the drafts are judged alone below
        final: dict[int, str] = {}
        for part in parts:
            chosen = None
            failures = []
            for text in (reviewed.get(part.id), drafts[part.id]):
                if text is None:
                    continue
                found = problems(part.source, text, translated_terms)
                if not found:
                    chosen = text
                    break
                failures.extend(found)
            if chosen is None:
                self.keep_source(unit, f"机械检查失败（{where(part)}）：" + "；".join(failures[:3]))
                return None
            final[part.id] = chosen
        unit.translations = [
            {"line": p.number, "part": p.kind, "source": p.source, "output": final[p.id]}
            for p in parts
            if p.has_text
        ]
        return final

    def _entries(self, unit: AlgorithmUnit) -> list[tuple[str, str]]:
        text = "\n".join(line.text for f in unit.fragments for line in f.lines).lower()
        return [
            (s, t)
            for s, t in self.glossary_entries
            if re.search(r"(?<![a-z0-9])" + re.escape(s.lower()) + r"(?![a-z0-9])", text)
        ]

    def _prompt(
        self,
        instructions: str,
        unit: AlgorithmUnit,
        section: str | None,
        entries: list[tuple[str, str]],
        items: list[dict],
    ) -> str:
        parts = [
            instructions,
            "## Paper dossier (condensed)\n" + self.condensed_dossier,
            "## Section of this algorithm\n" + (section or "(unknown)"),
        ]
        if entries:
            rows = "\n".join(f"| {s} | {t} |" for s, t in sorted(entries))
            parts.append(
                "## Glossary\n| Source Term | Target Term |\n|-------------|-------------|\n" + rows
            )
        source = "\n".join(
            (f"{line.number}: " if line.number is not None else "") + line.text
            for fragment in unit.fragments
            for line in fragment.lines
        )
        parts.append("## The whole algorithm (source, for context)\n" + source)
        parts.append("## Here is the input:\n\n" + json.dumps(items, ensure_ascii=False, indent=2))
        return "\n\n".join(parts)


def section_of(unit: AlgorithmUnit, paragraphs: list[tuple[int, object, str | None]]) -> str | None:
    """The section title in effect just before the algorithm.

    The last paragraph before it: on an earlier page, or on its page above
    it in the same column, or entirely left of it (the previous column).
    """
    fragment = unit.fragments[0]
    page = fragment.page - 1
    x0, _, x1, top = fragment.box
    section = None
    for number, box, title in paragraphs:
        if number < page:
            section = title
        elif number == page and box is not None:
            above = box.y >= top - 1 and box.x < x1 and box.x2 > x0
            left = box.x2 <= x0
            if above or left:
                section = title
    return section
