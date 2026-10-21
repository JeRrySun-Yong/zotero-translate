"""First-mention gloss plan: which paragraph asks for which key concept's gloss.

After the engine has recognised the paragraphs and before it translates any
of them, it hands the plan every paragraph in reading order: page number
(0-based), layout label and source text. The plan finds two ranges:

- **Abstract.** After a paragraph that reads "Abstract" (the heading), or
  from a paragraph that starts with "Abstract" and punctuation, up to the
  next heading. A paper without such a paragraph: the first long paragraph
  (``ABSTRACT_MIN_WORDS`` words or more) between the paper title and the
  next heading, together with the body-text paragraphs that directly follow
  it. The paper title is the first heading when no body-text paragraph
  comes before it. Otherwise the paper has no recognisable abstract.
- **Body.** From the first heading after the abstract (without an abstract:
  the first heading after the paper title; without a paper title: the
  start) up to the "References" heading.

Only body-text paragraphs are first-mention positions: headings, captions,
tables, footnotes, page furniture and text inside figures never are.

For each key concept, the first paragraph in the abstract and the first in
the body that contains one of its spellings get ``{"term", "gloss_zh"}``
(the gloss without a final full stop). A key concept without a gloss (a
promoted basic term whose gloss request failed) is skipped: it gets no
gloss and no placement, but its occurrences still cover shorter concepts.
Matching ignores case, whitespace and line-end hyphens, takes whole words
(a plural "s"/"es" allowed), and skips an occurrence that lies inside a
longer key concept's occurrence ("harness" inside "meta-harness") or inside
the source's own parentheses ("… models (MLLMs)"): a gloss there would nest
one pair of parentheses in another. The next occurrence is taken instead.

After translation the engine hands back the final text of each paragraph
that asked for glosses. A gloss counts as written when the final text,
without style tags and whitespace and NFKC-normalised (PDF text layers
hold CJK compatibility characters), contains the gloss normalised the same
way. ``placements`` then records this per concept and range, and
``written`` lists the planned glosses that were not written: the model may
leave a gloss out where the source explains the concept right there. This
is only recorded; nothing is translated again.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from zotero_translate.terms import KEY_CONCEPT

ABSTRACT_MIN_WORDS = 60
HEADING_LABELS = frozenset({"title", "paragraph_title"})
BODY_TEXT_LABELS = frozenset(
    {"plain text", "text", "paragraph_hybrid", "abstract", "content", "paragraph", "list_item"}
)
_ABSTRACT_HEADING = re.compile(r"^\W*abstract\W*$", re.IGNORECASE)
_ABSTRACT_INLINE = re.compile(r"^\W*abstract\s*[—–\-.:]", re.IGNORECASE)
_REFERENCES_HEADING = re.compile(
    r"^\W*(?:[0-9IVX]+\.?\s*)?(?:references|bibliography)\W*$", re.IGNORECASE
)

# Sentence punctuation at the end of a gloss, dropped: "（…。）" reads badly.
_GLOSS_END = "。．.；;，,！!"
_STYLE_TAG = re.compile(r"</?style[^>]*>")
RANGES = (("abstract", "abstract_page"), ("body", "body_page"))

Paragraph = tuple[int, str | None, str]  # page number (0-based), layout label, source text


@dataclass
class _Concept:
    first: str
    gloss_zh: str | None  # None: no gloss is planned, but its occurrences still cover shorter ones
    patterns: list[tuple[str, re.Pattern]]  # (spelling, pattern)


class FirstMentionPlan:
    """Called once by the engine before translation, and ``record_translations`` once after.

    ``placements`` and ``written`` then hold the task report's parts.
    """

    def __init__(self, terms: list[dict]):
        self._concepts = [
            _Concept(
                first=term["english"][0],
                gloss_zh=(term.get("gloss_zh") or "").strip().rstrip(_GLOSS_END) or None,
                patterns=[(s, _spelling_pattern(s)) for s in term["english"]],
            )
            for term in terms
            if term["category"] == KEY_CONCEPT
        ]
        self.placements: list[dict] = []
        self.written: dict | None = None
        self._planned: list[dict] = []  # {"concept", "term", "gloss_zh", "range", "page", "index"}

    def __call__(self, paragraphs: list[Paragraph]) -> dict[int, list[dict]]:
        """Paragraph index -> the glosses to ask for there, in concept order."""
        abstract, body = find_ranges(paragraphs)
        glosses: dict[int, list[dict]] = {}
        pages = {
            c.first: {
                "concept": c.first,
                "abstract_page": None,
                "body_page": None,
                "abstract_written": None,
                "body_written": None,
            }
            for c in self._concepts
            if c.gloss_zh
        }
        planned = []
        for (where, key), indices in zip(RANGES, (abstract, body)):
            pending = {n for n, c in enumerate(self._concepts) if c.gloss_zh}
            for index in indices:
                if not pending:
                    break
                page, label, text = paragraphs[index]
                if label not in BODY_TEXT_LABELS:
                    continue
                for number, spelling in self._mentions(text):
                    if number not in pending:
                        continue
                    pending.discard(number)
                    concept = self._concepts[number]
                    glosses.setdefault(index, []).append(
                        {"term": spelling, "gloss_zh": concept.gloss_zh}
                    )
                    pages[concept.first][key] = page + 1
                    planned.append(
                        {
                            "concept": concept.first,
                            "term": spelling,
                            "gloss_zh": concept.gloss_zh,
                            "range": where,
                            "page": page + 1,
                            "index": index,
                        }
                    )
        self.placements = list(pages.values())
        self._planned = planned
        return glosses

    def record_translations(self, finals: dict[int, str]) -> None:
        """Record which planned glosses ``finals`` (paragraph index -> final text) contain."""
        by_concept = {p["concept"]: p for p in self.placements}
        not_written = []
        for gloss in self._planned:
            final = finals.get(gloss["index"])
            written = final is not None and _normalised(gloss["gloss_zh"]) in _normalised(final)
            by_concept[gloss["concept"]][gloss["range"] + "_written"] = written
            if not written:
                not_written.append(
                    {k: gloss[k] for k in ("concept", "term", "gloss_zh", "range", "page")}
                    | {"translation": final}
                )
        self.written = {
            "planned": len(self._planned),
            "written": len(self._planned) - len(not_written),
            "not_written": not_written,
        }

    def _mentions(self, text: str) -> list[tuple[int, str]]:
        """``(concept number, spelling)`` for each concept that occurs in ``text``."""
        text = _squeeze(unicodedata.normalize("NFKC", text))
        parenthesised = _parenthesised(text)
        matches = [
            (m.start(), m.end(), number, spelling)
            for number, concept in enumerate(self._concepts)
            for spelling, pattern in concept.patterns
            for m in pattern.finditer(text)
        ]
        found: dict[int, str] = {}
        for start, end, number, spelling in sorted(matches):
            covered = any(
                s <= start and end <= e and e - s > end - start and n != number
                for s, e, n, _ in matches
            )
            inside = any(s < start and end <= e for s, e in parenthesised)
            if not covered and not inside:
                found.setdefault(number, spelling)
        return sorted(found.items())


def _parenthesised(text: str) -> list[tuple[int, int]]:
    """``(open, close)`` positions of each matched pair of parentheses in ``text``.

    An unmatched parenthesis ("1)" in a list, a pair split across
    paragraphs) encloses nothing.
    """
    pairs, opened = [], []
    for position, char in enumerate(text):
        if char in "(（":
            opened.append(position)
        elif char in ")）" and opened:
            pairs.append((opened.pop(), position))
    return pairs


def _normalised(text: str) -> str:
    """``text`` without style tags and whitespace, NFKC-normalised."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", _STYLE_TAG.sub("", text)))


def find_ranges(paragraphs: list[Paragraph]) -> tuple[range, range]:
    """The paragraph indices of the abstract and of the body (empty when not found)."""
    count = len(paragraphs)
    headings = [i for i, (_, label, text) in enumerate(paragraphs) if _is_heading(label, text)]
    end = next(
        (i for i in headings if _REFERENCES_HEADING.match(paragraphs[i][2].strip())), count
    )

    def next_heading(after: int) -> int | None:
        return next((i for i in headings if i > after), None)

    abstract = range(0)
    for i, (_, label, text) in enumerate(paragraphs[:end]):
        if label in {"fallback_line", "abandon"}:
            continue
        if _ABSTRACT_HEADING.match(text.strip()):
            start = i + 1
        elif label in BODY_TEXT_LABELS and _ABSTRACT_INLINE.match(text.strip()):
            start = i
        else:
            continue
        stop = next_heading(i)
        abstract = range(start, min(stop if stop is not None else end, end))
        break

    title = _paper_title(paragraphs, headings)
    if not abstract and title is not None:
        abstract = _headingless_abstract(paragraphs, title, next_heading(title), end)

    if abstract:
        body_start = next_heading(abstract.stop - 1)
    elif title is not None:
        body_start = next_heading(title)
    else:
        body_start = 0
    body = range(body_start, end) if body_start is not None else range(0)
    return abstract, body


def _paper_title(paragraphs: list[Paragraph], headings: list[int]) -> int | None:
    if not headings:
        return None
    first = headings[0]
    if any(label in BODY_TEXT_LABELS for _, label, _ in paragraphs[:first]):
        return None
    return first


def _headingless_abstract(
    paragraphs: list[Paragraph], title: int, following: int | None, end: int
) -> range:
    if following is None:
        return range(0)
    for i in range(title + 1, min(following, end)):
        _, label, text = paragraphs[i]
        if label in BODY_TEXT_LABELS and len(text.split()) >= ABSTRACT_MIN_WORDS:
            stop = i + 1
            while stop < following and paragraphs[stop][1] in BODY_TEXT_LABELS:
                stop += 1
            return range(i, stop)
    return range(0)


def _is_heading(label: str | None, text: str) -> bool:
    return label in HEADING_LABELS and bool(text.strip())


def _spelling_pattern(spelling: str) -> re.Pattern:
    """Whole-word, case-insensitive; tolerates line-end hyphens ("inspec- tion")."""
    spelling = _squeeze(unicodedata.normalize("NFKC", spelling))
    parts = []
    for char, after in zip(spelling, spelling[1:] + " "):
        if char == " ":
            parts.append(r"\s+")
        elif char == "-":
            parts.append(r"-\s?")
        elif char.isalnum() and after.isalnum():
            parts.append(re.escape(char) + r"(?:-\s)?")
        else:
            parts.append(re.escape(char))
    plural = r"(?:e?s)?" if spelling[-1:].isalpha() else ""
    return re.compile(
        r"(?<![A-Za-z0-9])" + "".join(parts) + plural + r"(?![A-Za-z0-9])", re.IGNORECASE
    )


def _squeeze(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
