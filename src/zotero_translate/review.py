"""Review: a second pass over each translated batch, with mechanical checks.

After a batch's draft translation, the engine calls a ``BatchReviewer``. It
sends one review request with the sources, the drafts, the condensed paper
dossier, the batch's glossary entries and the first-mention gloss fields; the
answer has the same JSON structure as the draft. Each paragraph then gets:

1. the reviewed text, if it passes the mechanical checks (``checks.py``);
2. otherwise the draft, if the draft passes;
3. otherwise the review request is resent, within the pipeline model
   client's retries for it (an answer with such a paragraph is rejected like
   one that does not parse, so it is never cached);
4. when the retries are used up, the paragraph keeps its source text and is
   recorded in ``KeptSource`` with its page and reason.

A review answer from the response cache is judged the same way. A review
request that gets no answer at all (network errors, HTTP errors, timeouts)
fails like a translation request: it is raised to the engine as
``TranslationRequestError``, which fails the paper task with the page.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

from babeldoc.babeldoc_exception.BabelDOCException import TranslationRequestError
from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import (
    GLOSS_PLACEMENT_RULES,
)
from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import (
    KEY_CONCEPT_HEADING_FIELD,
)
from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import (
    NO_ADDED_PARENTHESES_RULES,
)

from zotero_translate.batch_context import condense
from zotero_translate.checks import problems
from zotero_translate.model_client import ModelCallError
from zotero_translate.model_client import ModelClient

INSTRUCTIONS = f"""\
Review the draft translation of a batch of paragraphs from an English academic paper into Simplified Chinese, and return the revised translation.

## What to fix
- Mistranslations, omissions and additions compared with the source.
- Chinese that is unidiomatic or hard to read.
- Terminology: follow the glossary. A target term identical to its source term is a key concept of this paper: keep it in English exactly as written, never translate it.
{NO_ADDED_PARENTHESES_RULES}
- First-mention glosses: an item with "first_mention_glosses" has, at the first occurrence of each listed "term" in the item that is not inside parentheses (the source's own parentheses never hold a gloss), the term in English followed by its "gloss_zh" in full-width parentheses: term（gloss_zh）.
{GLOSS_PLACEMENT_RULES}
- Each gloss at most once; no gloss anywhere else. Do not add a gloss that the draft left out where the source explains the term right there.
If a draft is already right, return it unchanged.

## Keep exactly as in the source
- Every number, with the same digits: decimals, percentages, values with units. Do not convert units or write numbers in words.
- Placeholders such as {{v1}} and tags such as <style id='1'>…</style>: the same ones, each as often as in the source.
- URLs, code identifiers, model names and dataset names, character for character.

## Output format
A JSON array with one object per input item: {{"id": <the item's id>, "output": "<revised translation>"}}. No other fields, no text before or after, no ```json fences.
"""

# Added after the instructions only when an item has the field (closure ticket 17).
KEY_CONCEPT_HEADING_REVIEW_RULES = f"""\
## Section headings that are key concepts
An item with "{KEY_CONCEPT_HEADING_FIELD}" is a section heading that is the full name of a key concept: its translation is in Chinese only, the key concept included, with no English words, no gloss and no parentheses. For these items this overrides the terminology rule that keeps key concepts in English: do not put the English back."""


class KeptSource:
    """Paragraphs that keep their source text, with page and reason; thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self.paragraphs: list[dict] = []

    def record(self, page: int | None, source: str, reason: str) -> None:
        with self._lock:
            self.paragraphs.append({"page": page, "reason": reason, "source": source})


@dataclass
class _Verdict:
    text: str | None  # None: keep the source text
    used: str  # "review", "draft" or "source"
    reason: str | None = None


class BatchReviewer:
    """``reviewer(items, glossary_entries)`` -> {id: final text, or None to keep the source}.

    ``items`` are ``{"id", "source", "draft", "page"}`` dicts, plus
    ``"first_mention_glosses"`` where the batch asked for glosses and
    ``"key_concept_heading"`` where it asked for a heading in Chinese only;
    ``glossary_entries`` are the batch's ``(source term, target term)`` pairs.
    """

    def __init__(self, client: ModelClient, dossier: dict, kept: KeptSource):
        self.client = client
        self.condensed_dossier = condense(dossier)
        self.kept = kept
        self._lock = threading.Lock()
        self.batches = 0
        self.paragraphs = 0
        self.changes: list[dict] = []  # reviewed text used and different from the draft
        self.drafts_used: list[dict] = []  # review failed the checks, draft passed

    def __call__(self, items: list[dict], glossary_entries: list[tuple[str, str]]) -> dict:
        translated = frozenset(
            source.lower() for source, target in glossary_entries if source != target
        )
        messages = [{"role": "user", "content": self._prompt(items, glossary_entries)}]
        rejected: dict[int, _Verdict] = {}  # verdicts on the last rejected answer

        def judge(content: str) -> None:
            verdicts = _verdicts(items, content, translated)
            failing = [v.reason for v in verdicts.values() if v.text is None]
            if failing:
                rejected.clear()
                rejected.update(verdicts)
                raise ValueError(failing[0])

        try:
            # A cached answer skips ``judge``: it is judged below like a fresh one.
            content = self.client.complete(messages, validate=judge, page=items[0]["page"])
        except ModelCallError as e:
            if not e.answered:  # the request failed for good: the task fails
                raise TranslationRequestError(str(e)) from e
            verdicts = rejected or {
                item["id"]: _decide(item, None, translated) for item in items
            }  # no answer parsed: judge the drafts alone
        else:
            verdicts = _verdicts(items, content, translated)
        return self._apply(items, verdicts)

    def _apply(self, items: list[dict], verdicts: dict[int, _Verdict]) -> dict:
        result = {}
        with self._lock:
            self.batches += 1
            self.paragraphs += len(items)
            for item in items:
                verdict = verdicts[item["id"]]
                result[item["id"]] = verdict.text
                if verdict.used == "review" and verdict.text != item["draft"]:
                    self.changes.append(
                        {"page": item["page"], "draft": item["draft"], "review": verdict.text}
                    )
                elif verdict.used == "draft":
                    self.drafts_used.append({"page": item["page"], "reason": verdict.reason})
                elif verdict.used == "source":
                    self.kept.record(item["page"], item["source"], verdict.reason)
        return result

    def report(self) -> dict:
        with self._lock:
            return {
                "batches": self.batches,
                "paragraphs": self.paragraphs,
                "changed_paragraphs": len(self.changes),
                "drafts_used": list(self.drafts_used),
                "changes": list(self.changes),
            }

    def _prompt(self, items: list[dict], glossary_entries: list[tuple[str, str]]) -> str:
        parts = [INSTRUCTIONS]
        if any(KEY_CONCEPT_HEADING_FIELD in item for item in items):
            parts.append(KEY_CONCEPT_HEADING_REVIEW_RULES)
        parts.append("## Paper dossier (condensed)\n" + self.condensed_dossier)
        if glossary_entries:
            rows = "\n".join(f"| {s} | {t} |" for s, t in sorted(glossary_entries))
            parts.append(
                "## Glossary\n| Source Term | Target Term |\n|-------------|-------------|\n" + rows
            )
        fields = ("id", "source", "draft", "first_mention_glosses", KEY_CONCEPT_HEADING_FIELD)
        payload = [{k: item[k] for k in fields if k in item} for item in items]
        parts.append(
            "## Here is the input:\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)
        )
        return "\n\n".join(parts)


def _verdicts(items: list[dict], content: str, translated: frozenset[str]) -> dict[int, _Verdict]:
    """Each item's verdict on a review answer; raise if the answer does not parse."""
    outputs = _parse(content)
    return {item["id"]: _decide(item, outputs.get(item["id"]), translated) for item in items}


def _decide(item: dict, reviewed: str | None, translated: frozenset[str]) -> _Verdict:
    if isinstance(reviewed, str):
        review_problems = problems(item["source"], reviewed, translated)
        if not review_problems:
            return _Verdict(reviewed, "review")
    else:
        review_problems = ["审校结果缺失或无法解析"]
    draft_problems = problems(item["source"], item["draft"], translated)
    if not draft_problems:
        return _Verdict(item["draft"], "draft", "审校结果未通过机械检查：" + "；".join(review_problems))
    return _Verdict(
        None,
        "source",
        "审校结果与初译都未通过机械检查：审校 "
        + "；".join(review_problems)
        + "；初译 "
        + "；".join(draft_problems),
    )


def _parse(content: str) -> dict:
    """``{id: output}`` of a review answer; raise if it does not parse."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    value = json.loads(text)
    if not isinstance(value, list):
        raise ValueError("审校结果不是 JSON 数组")
    return {
        item["id"]: item.get("output")
        for item in value
        if isinstance(item, dict) and isinstance(item.get("id"), int)
    }
