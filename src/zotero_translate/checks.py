"""Mechanical checks: numbers, formula placeholders and literals a translation must keep.

``problems(source, translation)`` compares one paragraph's source text (as the
engine sends it, with ``{v1}`` formula placeholders and ``<style id='1'>``
tags) with a translation of it and returns what the translation changed, as
short Chinese reasons. An empty list means the translation passes.

- Numbers: every number of the source (integers, decimals, percentages, the
  value of a quantity with a unit) occurs in the translation at least as
  often. Thousands separators are ignored; a percentage must stay a
  percentage. Digits joined to letters (``3D``, ``1st``, ``GPT-4``) are not
  numbers: they are literals or ordinary words.
- Formula placeholders: the translation has the same placeholders, each as
  often as the source (same count, same ids).
- Literals: URLs, e-mail addresses, code identifiers (``snake_case``,
  ``call()``, file names), and names that mix letters and digits with a
  capital (``GPT-5.5``, ``H100``, and versions written as a word and digits:
  ``Qwen2.5-VL``, ``Wan2.1``, ``Veo3``) occur in the translation character
  for character, whitespace ignored: the translation loses its whitespace
  before the comparison (a literal never contains any), so ``Wan 2.1``
  keeps ``Wan2.1``. Spellings the paper's glossary translates are not
  checked.

The checks are deliberately conservative: a false alarm makes a paragraph
fall back to its draft, be resent, or keep its source text. CamelCase names
(``PosterBench``) and acronyms (``LLMs``) are not checked.

The engine's paragraph text used to lack the space between two words
(``PosterBenchScore``, ``Section3.2``, ``...ac.aeWork``). Closure ticket 02
fixed the engine's word spaces (``Appendix H.1``, ``Introducing GPT-5.5``,
``Fig. 4`` now keep their space), so the rules for glued words are gone:

- ticket 12's leading capitalised word glued to a name (``AppendixH.1``,
  ``Fig.4``): a source that really writes ``Fig.4`` without a space needs
  ``Fig.4`` in the translation;
- the exemption of a word directly followed by digits (``Section3.2``,
  commit 34d113f; closure ticket 09, red team D1), which let a changed
  version (``Wan2.1`` written as ``Wan2.2``) pass. A superscript after an
  author's name is a formula placeholder in the engine's text
  (``Yaxin Luo{v1}``), so it never makes the name a literal;
- the stopgap that let a literal pass without a glued-on word (M1 ticket
  12; closure ticket 09): a leading lower-case word (``theGPT-4``), a
  trailing lower-case word (``GPT-5.5and``) or a trailing capitalised word
  (``...ac.aeWork``).
"""

from __future__ import annotations

import collections
import re

_FORMULA = re.compile(r"\{\s*v\s*(\d+)\s*\}", re.IGNORECASE)
_STYLE_TAG = re.compile(r"<\s*/?\s*style[^>]*>", re.IGNORECASE)

_LITERALS = [
    re.compile(r"(?:https?://|www\.)[^\s<>\"'　-〿＀-￯一-鿿]+"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
    # snake_case and calls
    re.compile(r"[A-Za-z_][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"),
    re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\(\)"),
    # file names
    re.compile(
        r"[A-Za-z0-9_-]+\.(?:py|json|jsonl|yaml|yml|md|txt|js|ts|sh|cpp|ipynb|csv|html|tex)\b"
    ),
    # names mixing letters and digits, with a capital: GPT-5.5, Qwen2.5-VL, H100
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)*"),
]
_NAME = _LITERALS[-1]
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""
_WHITESPACE = re.compile(r"\s+")

_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(\d+(?:,\d{3})*(?:\.\d+)?)(\s*[%％])?(?![A-Za-z0-9_])")


def problems(source: str, translation: str, translated_terms: frozenset[str] = frozenset()) -> list[str]:
    """What ``translation`` changed of ``source``'s numbers, formulas and literals.

    ``translated_terms`` are lower-case spellings the paper's glossary
    translates into Chinese; they are not checked as literals.
    """
    found: list[str] = []
    source_formulas = collections.Counter(_FORMULA.findall(source))
    target_formulas = collections.Counter(_FORMULA.findall(translation))
    if source_formulas != target_formulas:
        found.append(
            "公式占位符不一致：原文 "
            + _formula_list(source_formulas)
            + "，译文 "
            + _formula_list(target_formulas)
        )
    source_text, target_text = _plain(source), _plain(translation)
    literals = [lit for lit in _literals(source_text) if lit.lower() not in translated_terms]
    target_squeezed = _WHITESPACE.sub("", target_text)
    source_numbers = _numbers(source_text, literals)
    target_numbers = _numbers(target_text, literals)
    for literal in dict.fromkeys(literals):
        if literal not in target_squeezed:
            found.append(f"字面值 {literal} 被改动或缺失")
    for number, count in source_numbers.items():
        if target_numbers[number] < count:
            found.append(f"数字 {number} 被改动或缺失")
    return found


def _plain(text: str) -> str:
    """Text without style tags; formula placeholders become spaces."""
    return _FORMULA.sub(" ", _STYLE_TAG.sub("", text))


def _literals(text: str) -> list[str]:
    found = []
    for pattern in _LITERALS:
        for match in pattern.finditer(text):
            literal = match.group(0).rstrip(_TRAILING_PUNCTUATION)
            if pattern is _NAME and (
                not re.search(r"\d", literal) or not re.search(r"[A-Z]", literal)
            ):
                continue
            if len(literal) > 1:
                found.append(literal)
    return found


def _numbers(text: str, literals: list[str]) -> collections.Counter:
    for literal in sorted(set(literals), key=len, reverse=True):
        text = text.replace(literal, " ")
    return collections.Counter(_number(match) for match in _NUMBER.finditer(text))


def _number(match: re.Match) -> str:
    """A number as the checks compare it: no thousands separators; % kept."""
    return match.group(1).replace(",", "") + ("%" if match.group(2) else "")


def _formula_list(counter: collections.Counter) -> str:
    return " ".join(f"{{v{i}}}" * n for i, n in sorted(counter.items())) or "无"
