"""A fixed sample translation batch prompt and review prompt (M4 ticket 15).

``tests/test_prompt_protections.py`` pins the built-in Gemini profile's
request bodies of these two prompts by their cache keys, computed before
ticket 15 on the integration branch (ce39e0b, whose ``engine/`` and ``src/``
are a0f8ad4's): the user profiles' prompt rules must leave them unchanged.
"""
import json
from types import SimpleNamespace

from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import ILTranslatorLLMOnly
from babeldoc.glossary import Glossary, GlossaryEntry

from zotero_translate.model_client import ResponseCache
from zotero_translate.review import BatchReviewer, KeptSource

DOSSIER = {
    "paper_title": "A Sample Paper on Structured Outputs",
    "discipline": "计算机科学",
    "paper_summary_zh": "本文研究结构化输出。",
    "section_summaries_zh": [{"section": "1 Introduction", "summary_zh": "引言。"}],
    "translation_pitfalls_zh": ["status 字段的取值保持英文。"],
}
ITEMS = [
    {
        "id": 0,
        "input": "The planner sets {v1} to one of {low, medium, high} and writes \"status\": \"ready\" when <style id='2'>done</style>.",
        "layout_label": "text",
        "first_mention_glosses": [{"term": "planner", "gloss_zh": "制定步骤的模块"}],
    },
    {"id": 1, "input": "3 Structured Planner", "layout_label": "title", "key_concept_heading": "Structured Planner"},
]
GLOSSARY = [("planner", "planner"), ("status field", "状态字段")]
CONTEXT = "## Paper context\nTitle: A Sample Paper on Structured Outputs\n### Section\n3 Structured Planner"


def body(prompt: str) -> dict:
    """The built-in flash role's request body (``ModelClient._body``, streaming off)."""
    return {
        "model": "gemini-3.8-flash-high",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 1.0,
        "reasoning_effort": "high",
    }


def batch_prompt(**config) -> str:
    translator = object.__new__(ILTranslatorLLMOnly)
    translator.translation_config = SimpleNamespace(lang_out="zh", custom_system_prompt=None, **config)
    translator._cached_glossaries = [Glossary("terms", [GlossaryEntry(s, t, "zh") for s, t in GLOSSARY])]
    text = "\n".join(item["input"] for item in ITEMS)
    return translator._build_llm_prompt(
        json_input_str=json.dumps(ITEMS, ensure_ascii=False, indent=2),
        title_paragraph=None,
        local_title_paragraph=None,
        batch_text_for_glossary_matching=text,
        paper_context_block=CONTEXT,
        has_first_mention_glosses=True,
        has_key_concept_headings=True,
    )


def review_prompt(**options) -> str:
    reviewer = BatchReviewer(None, DOSSIER, KeptSource(), **options)
    items = [
        {
            "id": 0,
            "source": ITEMS[0]["input"],
            "draft": "规划器将 {v1} 设为 {low, medium, high} 之一，并在<style id='2'>完成</style>时写入 \"status\": \"ready\"。",
            "page": 3,
            "first_mention_glosses": ITEMS[0]["first_mention_glosses"],
        },
        {"id": 1, "source": ITEMS[1]["input"], "draft": "3 结构化规划器", "page": 3, "key_concept_heading": "Structured Planner"},
    ]
    return reviewer._prompt(items, GLOSSARY)


def key(prompt: str) -> str:
    return ResponseCache.key(body(prompt))
