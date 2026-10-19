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
  capital (``GPT-5.5``, ``H100``, ``Qwen2.5-VL``) occur in the translation
  character for character, whitespace ignored: the translation loses its
  whitespace before the comparison (a literal never contains any).
  Spellings the paper's glossary translates are not checked.

The checks are deliberately conservative: a false alarm makes a paragraph
fall back to its draft, be resent, or keep its source text. The engine's
paragraph text often lacks the space between two words
(``PosterBenchScore``, ``Section3.2``, ``...ac.aeWork``), so:

- CamelCase names (``PosterBench``) and acronyms (``LLMs``) are not checked;
- a word directly followed by digits (``Section3.2``, ``Qwen2``) is not a
  literal;
- a literal also passes when the translation has it without a glued-on
  word: a leading lower-case word before a capital (``theGPT-4``), a
  leading capitalised word, or abbreviation with its dot, before what
  starts like a name of its own (``IntroducingGPT-5.5``, ``AppendixH.1``,
  ``Fig.4``; the translation writes "Introducing GPT-5.5", "附录 H.1",
  "图 4"), a trailing lower-case word of 2 or more letters after a capital
  or digit (``GPT-5.5and``), or a trailing capitalised word after a
  lower-case letter (``...ac.aeWork``). What is left of ``Fig.4`` is a
  number: it must be a whole number of the translation, so "图 14" and
  "图 5" fail as they would for ``Fig. 4``.
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
_WORD_THEN_DIGITS = re.compile(r"[A-Za-z][a-z]+\d")
_GLUED = [
    re.compile(r"^[a-z]+(?=[A-Z])"),
    re.compile(r"^[A-Z][a-z]+(?:\.(?=\d)|(?=[A-Z][A-Z0-9.-]))"),
    re.compile(r"(?<=[A-Z0-9])[a-z]{2,}$"),
    re.compile(r"(?<=[a-z])[A-Z][a-z]+$"),
]
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
    target_whole_numbers = {m.group(1).replace(",", "") for m in _NUMBER.finditer(target_text)}
    for literal in dict.fromkeys(literals):
        if not any(
            variant in (target_whole_numbers if variant.isdigit() else target_squeezed)
            for variant in _variants(literal)
        ):
            found.append(f"字面值 {literal} 被改动或缺失")
    source_numbers = _numbers(source_text, literals)
    target_numbers = _numbers(target_text, literals)
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
                not re.search(r"\d", literal)
                or not re.search(r"[A-Z]", literal)
                or _WORD_THEN_DIGITS.match(literal)
            ):
                continue
            if len(literal) > 1:
                found.append(literal)
    return found


def _variants(literal: str) -> list[str]:
    """The literal, and the literal without each kind of glued-on word.

    A variant may be a lone digit only when an abbreviation was glued to a
    number (``Fig.4``); it is then matched as a whole number.
    """
    variants = [literal]
    for glued in _GLUED:
        shorter = glued.sub("", literal)
        if shorter != literal and (len(shorter) > 1 or shorter.isdigit()):
            variants.append(shorter)
    return variants


def _numbers(text: str, literals: list[str]) -> collections.Counter:
    for literal in sorted(set(literals), key=len, reverse=True):
        text = text.replace(literal, " ")
    numbers = collections.Counter()
    for match in _NUMBER.finditer(text):
        value = match.group(1).replace(",", "")
        numbers[value + ("%" if match.group(2) else "")] += 1
    return numbers


def _formula_list(counter: collections.Counter) -> str:
    return " ".join(f"{{v{i}}}" * n for i, n in sorted(counter.items())) or "无"
