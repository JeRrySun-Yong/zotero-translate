import json
import logging
import re
import unicodedata
from pathlib import Path
from string import Template

import Levenshtein
import tiktoken
from tqdm import tqdm

from babeldoc.babeldoc_exception.BabelDOCException import TranslationRequestError
from babeldoc.format.pdf.document_il import Document
from babeldoc.format.pdf.document_il import Page
from babeldoc.format.pdf.document_il import PdfFont
from babeldoc.format.pdf.document_il import PdfParagraph
from babeldoc.format.pdf.document_il.midend import il_translator
from babeldoc.format.pdf.document_il.midend.il_translator import (
    DocumentTranslateTracker,
)
from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
from babeldoc.format.pdf.document_il.midend.il_translator import PageTranslateTracker
from babeldoc.format.pdf.document_il.midend.il_translator import (
    ParagraphTranslateTracker,
)
from babeldoc.format.pdf.document_il.utils.fontmap import FontMapper
from babeldoc.format.pdf.document_il.utils.paragraph_helper import is_cid_paragraph
from babeldoc.format.pdf.document_il.utils.paragraph_helper import (
    is_placeholder_only_paragraph,
)
from babeldoc.format.pdf.document_il.utils.paragraph_helper import (
    is_pure_numeric_paragraph,
)
from babeldoc.format.pdf.translation_config import TitleContextSnapshot
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.translator.translator import BaseTranslator
from babeldoc.utils.priority_thread_pool_executor import PriorityThreadPoolExecutor

logger = logging.getLogger(__name__)


PROMPT_TEMPLATE = Template(
    """$role_block

## Structure Rules
1. Keep **the same number of paragraphs as the input**.
2. Input paragraphs may be **sliced pieces of the same original paragraph**.  
   → You MUST treat each input paragraph **as an independent, fixed unit**.  
   → Do NOT merge paragraphs, split paragraphs, or move content between paragraphs.
3. Inside each paragraph, you may adjust word order for fluency, but:
   - Do NOT change the meaning.
   - Do NOT move placeholders, tags, or code outside their paragraph.
4. Translate ALL human-readable content into $lang_out.

## Do NOT Modify
- Tags (e.g., <style>, <b>, <code>): keep them exactly the same.  
  *Translate tag-internal text except code blocks (<code>…</code>)*.
- Placeholders: `{v1}`, `{name}`, `%s`, `%d`, `[[...]]`, `%%...%%` — keep exactly unchanged.
- Numbers (decimals, percentages, values with units), URLs, code identifiers, model names and dataset names: keep them exactly as in the input.
- JSON keys or structure.

$glossary_usage_rules_block$first_mention_rules_block$key_concept_heading_rules_block$batch_extra_rules_block
## Output Format
Return a JSON array of the same length.  
For each item:
- Keep the same "id" and remove other fields like "input" and "layout_label".
- Add "output" with the translated text only.
- No extra text, no ```json blocks.

## Style
- Produce fluent, professional $lang_out.
- Preserve punctuation unless needed for target language fluency.
$no_added_parentheses_rules

### Example
Input:
[
    {
    "id": 0,
    "input": "{v1}<style id='2'>hello</style>, world!",
    "layout_label": "text"
    }
]
Output:
[
    {
    "id": 0,
    "output": "{v1}<style id='2'>你好</style>，世界！"
    }
]

$contextual_hints_block

$glossary_tables_block

## Here is the input:

$json_input_str"""
)


FIRST_MENTION_FIELD = "first_mention_glosses"

GLOSS_PLACEMENT_RULES = """- Put the gloss after the whole English phrase that contains the term, never inside the phrase: self-consistency decoding（gloss_zh）, not self-consistency（gloss_zh） decoding.
- Write the gloss outside style tags, so that it does not take the term's bold or italic: when the term or its phrase ends a <style> tag, put the gloss right after the closing tag: <style id='1'>term</style>（gloss_zh）, not <style id='1'>term（gloss_zh）</style>.
- Where the source right there already explains the term (a definition, an apposition or a list of its parts right after it), you may leave the gloss out."""

FIRST_MENTION_RULES = f"""## First-Mention Glosses
Some input items have "{FIRST_MENTION_FIELD}": key concepts of this paper that occur in that item for the first time in the abstract or in the body. In the output of such an item:
- At the first occurrence of each listed "term" in the item that is not inside parentheses, keep the term in English exactly as written there, and put its "gloss_zh" in full-width parentheses after it, with no space: term（gloss_zh）. Example: agentic（自主规划与执行能力）.
- Skip an occurrence inside the input's own parentheses: in "… Adaptive Query Router (AQR) module. The AQR module …", the gloss goes after the second AQR, never inside "(AQR)".
{GLOSS_PLACEMENT_RULES}
- Add each gloss at most once. Add no gloss anywhere else, and do not output the "{FIRST_MENTION_FIELD}" field.

"""

KEY_CONCEPT_HEADING_FIELD = "key_concept_heading"

# A section heading that is a key concept's full name is translated into
# Chinese only, although the glossary keeps key concepts in English
# (requirement 1; user's decision of 2026-10-29, closure ticket 17).
KEY_CONCEPT_HEADING_TRANSLATION_RULES = f"""## Section Headings That Are Key Concepts
Some input items have "{KEY_CONCEPT_HEADING_FIELD}": the item is a section heading that is the full name of a key concept of this paper. Translate such a heading into Chinese, the key concept included, and write it in Chinese only: no English words, no gloss and no parentheses; keep its section number. This overrides, for these items only, the glossary rule that keeps key concepts in English. Do not output the "{KEY_CONCEPT_HEADING_FIELD}" field.

"""

# Parentheses the source does not have make the translation hard to read.
NO_ADDED_PARENTHESES_RULES = """- Do not add the English source words after Chinese text, as in 鲁棒性（Robustness）: write the Chinese alone, or the English alone where the glossary keeps a term in English.
- Do not turn the source's commas, dashes or appositives into parentheses: an aside or apposition set off by commas or dashes stays set off by commas or dashes."""


# Short labels (M4 ticket 15, from ticket 14). Below this many input tokens
# an input is usually a label (a table header, a figure label): a Chinese
# label easily tokenizes to three times the English one's tokens, so the
# token ratio check alone rejects good translations ("References" ->
# 参考图像) and the simple redo then picks a worse word (参考文献).
LENGTH_RATIO_MIN_TOKENS = 10

# A label's Chinese may take up to this factor of the source's width, with
# room for three Chinese characters, or six for an abbreviation or acronym
# ("mAP" -> 平均精度均值). Widths count a full-width character as two.
SHORT_LABEL_MAX_WIDTH_RATIO = 1.2
SHORT_LABEL_MIN_WIDTH = 6
ABBREVIATION_MAX_WIDTH = 12
# An acronym is short; a longer all-capitals word (OBJECT) is an ordinary
# word set in capitals and gets the ordinary allowance.
ACRONYM_MAX_LETTERS = 5

# Past twice the source's width, or six Chinese characters for a tiny
# source, an output the token ratio rejects is a runaway answer ("甲" * 100)
# or the source repeated ("Object " * 30), not a label's translation. It
# never vetoes an output the ratio check passes.
SHORT_OUTPUT_MAX_WIDTH_RATIO = 2
SHORT_OUTPUT_MIN_WIDTH = 12

_STYLE_TAG = re.compile(r"<\s*/?\s*style[^>]*>", re.IGNORECASE)


def display_width(text: str) -> int:
    """Approximate rendered width: a full-width character counts as two."""
    return sum(
        2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
        for character in text
    )


def translated_width(text: str) -> int:
    """Width of the full-width text, the part translating a label adds.

    Latin text kept in a translation (a model name, a key concept) is not
    growth from translating the label, so it is not counted.
    """
    return sum(
        2 if unicodedata.east_asian_width(character) in ("W", "F") else 0
        for character in text
    )


def _looks_like_abbreviation(text: str) -> bool:
    """A one-word abbreviation ("Inst.") or acronym ("BG", "mAP"): at least
    two capitals and at most ACRONYM_MAX_LETTERS letters."""
    word = text.strip()
    if not word or " " in word:
        return False
    letters = [character for character in word if character.isalpha()]
    if not letters:
        return False
    if word.endswith("."):
        return True
    capitals = sum(1 for character in letters if character.isupper())
    return capitals >= 2 and len(letters) <= ACRONYM_MAX_LETTERS


def translation_length_ok(
    input_text: str,
    output_text: str,
    input_token_count: int,
    output_token_count: int,
) -> bool:
    """Whether the output length is acceptable for this input.

    The upstream check: the output/input token ratio is between 0.3 and 3.
    An input under LENGTH_RATIO_MIN_TOKENS (a label) is only let through
    more often than by that check, never less (M4 ticket 15), except for an
    output that translates nothing. Its output, style tags left out, fails
    when it is blank, or when the source has letters and the output has no
    letter or digit (only punctuation: "." for a sentence).

    Otherwise it passes when the token ratio passes (however wide:
    "MLLMs" -> 多模态大语言模型), or, when the ratio fails, when it fits the
    label:

    - the whole output is at most twice the source's width, or six Chinese
      characters for a tiny source, so that a runaway answer ("甲" * 100) or
      the source repeated ("Object " * 30) is not taken for a label;
    - and its Chinese part is at most 1.2 times the source's width, with
      room for three Chinese characters (six for an abbreviation):
      "References" -> 参考图像 passes, "Object" -> 物体组合 and "Global" ->
      全局风格 (AutoRef's table headers expanded from the paper dossier,
      rendered at half size) do not.

    A label that fails is redone with the simple one-paragraph prompt, which
    has no dossier.
    """
    ratio_ok = input_token_count > 0 and (
        0.3 < output_token_count / input_token_count < 3
    )
    if input_token_count >= LENGTH_RATIO_MIN_TOKENS:
        return ratio_ok
    source = _STYLE_TAG.sub("", input_text).strip()
    output = _STYLE_TAG.sub("", output_text).strip()
    if not output:
        return False
    has_letters = any(character.isalpha() for character in source)
    if has_letters and not any(character.isalnum() for character in output):
        return False
    if ratio_ok:
        return True
    # The width rule, for a label the ratio check rejects.
    source_width = display_width(source)
    if display_width(output) > max(
        SHORT_OUTPUT_MIN_WIDTH, source_width * SHORT_OUTPUT_MAX_WIDTH_RATIO
    ):
        return False
    if not has_letters:
        return True
    room = (
        ABBREVIATION_MAX_WIDTH
        if _looks_like_abbreviation(source)
        else SHORT_LABEL_MIN_WIDTH
    )
    return translated_width(output) <= max(
        room, source_width * SHORT_LABEL_MAX_WIDTH_RATIO
    )


class BatchParagraph:
    def __init__(
        self,
        paragraphs: list[PdfParagraph],
        pages: list[Page],
        page_tracker: PageTranslateTracker,
    ):
        self.paragraphs = paragraphs
        self.pages = pages
        self.trackers = [page_tracker.new_paragraph() for _ in paragraphs]


class ILTranslatorLLMOnly:
    stage_name = "Translate Paragraphs"

    def __init__(
        self,
        translate_engine: BaseTranslator,
        translation_config: TranslationConfig,
        tokenizer=None,
    ):
        self.translate_engine = translate_engine
        self.translation_config = translation_config
        self.font_mapper = FontMapper(translation_config)
        self.shared_context_cross_split_part = (
            translation_config.shared_context_cross_split_part
        )

        if tokenizer is None:
            self.tokenizer = tiktoken.encoding_for_model("gpt-4o")
        else:
            self.tokenizer = tokenizer

        # Cache glossaries at initialization
        self._cached_glossaries = (
            self.shared_context_cross_split_part.get_glossaries_for_translation(
                translation_config.auto_extract_glossary
            )
        )

        self.il_translator = ILTranslator(
            translate_engine=translate_engine,
            translation_config=translation_config,
            tokenizer=self.tokenizer,
        )
        self.il_translator.use_as_fallback = True
        try:
            self.translate_engine.do_llm_translate(None)
        except NotImplementedError as e:
            raise ValueError("LLM translator not supported") from e

        self.ok_count = 0
        self.fallback_count = 0
        self.total_count = 0

    def calc_token_count(self, text: str) -> int:
        try:
            return len(self.tokenizer.encode(text, disallowed_special=()))
        except Exception:
            return 0

    def find_title_paragraph(self, docs: Document) -> PdfParagraph | None:
        """Find the first paragraph with layout_label 'title' in the document.

        Args:
            docs: The document to search in

        Returns:
            The first title paragraph found, or None if no title paragraph exists
        """
        for page in docs.page:
            for paragraph in page.pdf_paragraph:
                if paragraph.layout_label == "title":
                    logger.info(f"Found title paragraph: {paragraph.unicode}")
                    return paragraph
        return None

    def translate(self, docs: Document) -> None:
        self.il_translator.docs = docs
        tracker = DocumentTranslateTracker()
        self.mid = 0
        self._index_reading_order(docs)
        self._start_algorithm_translation(docs)

        if not self.translation_config.shared_context_cross_split_part.first_paragraph:
            # Try to find the first title paragraph
            title_paragraph = self.find_title_paragraph(docs)
            self.translation_config.shared_context_cross_split_part.first_paragraph = (
                self.shared_context_cross_split_part.snapshot_title_paragraph(
                    title_paragraph
                )
            )
            self.translation_config.shared_context_cross_split_part.recent_title_paragraph = self.shared_context_cross_split_part.snapshot_title_paragraph(
                title_paragraph
            )
            if title_paragraph:
                logger.info(f"Found first title paragraph: {title_paragraph.unicode}")

        # count total paragraph
        total = sum(
            [
                len(
                    [
                        p
                        for p in page.pdf_paragraph
                        if p.debug_id is not None and p.unicode is not None
                    ]
                )
                for page in docs.page
            ]
        )
        translated_ids = set()
        with self.translation_config.progress_monitor.stage_start(
            self.stage_name,
            total,
        ) as pbar:
            with PriorityThreadPoolExecutor(
                max_workers=self.translation_config.pool_max_workers,
            ) as executor2:
                with PriorityThreadPoolExecutor(
                    max_workers=self.translation_config.pool_max_workers,
                ) as executor:
                    self.process_cross_page_paragraph(
                        docs,
                        executor,
                        pbar,
                        tracker,
                        executor2,
                        translated_ids,
                    )
                    # Cross-column detection per page (after cross-page processing)
                    for page in docs.page:
                        self.process_cross_column_paragraph(
                            page,
                            executor,
                            pbar,
                            tracker,
                            executor2,
                            translated_ids,
                        )
                    for page in docs.page:
                        self.process_page(
                            page,
                            executor,
                            pbar,
                            tracker.new_page(),
                            executor2,
                            translated_ids,
                        )
        # A batch whose request failed for good fails the stage instead of
        # leaving its paragraphs in the source language.
        self.il_translator.raise_if_request_failed()
        self._record_first_mention_translations(docs)

        path = self.translation_config.get_working_file_path("translate_tracking.json")

        if (
            self.translation_config.debug
            or self.translation_config.working_dir is not None
        ):
            logger.debug(f"save translate tracking to {path}")
            with Path(path).open("w", encoding="utf-8") as f:
                f.write(tracker.to_json())
        logger.info(
            f"Translation completed. Total: {self.total_count}, Successful: {self.ok_count}, Fallback: {self.fallback_count}"
        )

    # Text outside the layout regions (labels inside figures) and page
    # furniture (running heads, page numbers) is not neighbouring context.
    NOT_CONTEXT_LABELS = frozenset({"fallback_line", "abandon"})

    def _index_reading_order(self, docs: Document) -> None:
        """Snapshot every paragraph's source text and section title.

        Taken before any batch is sent: translated paragraphs get their
        translation as text, and a batch's neighbours must stay source text.
        A section starts at each paragraph labelled "title".
        """
        self._reading_order: dict[int, int] = {}
        self._source_texts: list[str | None] = []  # None: not context
        self._section_titles: list[str | None] = []
        paragraphs: list[tuple[int, str | None, str]] = []
        section = None
        for page in docs.page:
            for paragraph in page.pdf_paragraph:
                text = (paragraph.unicode or "").strip()
                if paragraph.layout_label == "title" and text:
                    section = text
                is_context = text and paragraph.layout_label not in self.NOT_CONTEXT_LABELS
                self._reading_order[id(paragraph)] = len(self._source_texts)
                self._source_texts.append(text if is_context else None)
                self._section_titles.append(section)
                paragraphs.append((page.page_number, paragraph.layout_label, text))
        planner = self.translation_config.first_mention_planner
        self._first_mention_glosses = planner(paragraphs) if planner else {}
        headings = self.translation_config.key_concept_headings
        self._key_concept_headings = headings(paragraphs) if headings else {}

    def _start_algorithm_translation(self, docs: Document) -> None:
        """Hand every paragraph's page, box and section title to the algorithm translator."""
        hook = self.translation_config.algorithm_translator
        if hook is None:
            return
        paragraphs = []
        for page in docs.page:
            for paragraph in page.pdf_paragraph:
                position = self._reading_order.get(id(paragraph))
                if position is None or paragraph.box is None:
                    continue
                paragraphs.append(
                    (page.page_number, paragraph.box, self._section_titles[position])
                )
        hook(paragraphs)

    def _record_first_mention_translations(self, docs: Document) -> None:
        """Hand the final text of each paragraph that asked for glosses to the recorder."""
        recorder = self.translation_config.first_mention_recorder
        if recorder is None:
            return
        finals: dict[int, str] = {}
        for page in docs.page:
            for paragraph in page.pdf_paragraph:
                position = self._reading_order.get(id(paragraph))
                if position in self._first_mention_glosses:
                    finals[position] = paragraph.unicode or ""
        recorder(finals)

    def _first_mention_glosses_of(self, paragraph: PdfParagraph) -> list[dict]:
        position = self._reading_order.get(id(paragraph))
        return self._first_mention_glosses.get(position, [])

    def _key_concept_heading_of(self, paragraph: PdfParagraph) -> str | None:
        position = self._reading_order.get(id(paragraph))
        return self._key_concept_headings.get(position)

    def _paper_context_block(self, paragraphs: list[PdfParagraph]) -> str | None:
        """The paper context of a batch, or None without a context builder.

        The builder gets the titles of the sections the batch spans and the
        source texts of the paragraphs before and after it.
        """
        builder = self.translation_config.batch_context_builder
        if builder is None:
            return None
        positions = [
            self._reading_order[id(p)] for p in paragraphs if id(p) in self._reading_order
        ]
        if not positions:
            return builder([], [], [])
        first, last = min(positions), max(positions)
        titles: list[str] = []
        for title in self._section_titles[first : last + 1]:
            if title is not None and title not in titles:
                titles.append(title)
        return builder(
            titles,
            [t for t in self._source_texts[:first] if t is not None],
            [t for t in self._source_texts[last + 1 :] if t is not None],
        )

    def _is_body_text_paragraph(self, paragraph: PdfParagraph) -> bool:
        """判断正文段落（当前仅 layout_label == 'text'）。

        Args:
            paragraph: PDF paragraph to check

        Returns:
            True if this is a body text paragraph, False otherwise
        """
        return paragraph.layout_label in (
            "text",
            "plain text",
            "paragraph_hybrid",
        )

    def _should_translate_paragraph(
        self,
        paragraph: PdfParagraph,
        translated_ids: set[int] | None = None,
        require_body_text: bool = False,
    ) -> bool:
        """Check if a paragraph should be translated based on common filtering criteria.

        Args:
            paragraph: PDF paragraph to check
            translated_ids: Set of already translated paragraph IDs
            require_body_text: Whether to additionally check if paragraph is body text

        Returns:
            True if paragraph should be translated, False otherwise
        """
        # Basic validation checks
        if paragraph.debug_id is None or paragraph.unicode is None:
            return False

        # Check if already translated
        if translated_ids is not None and id(paragraph) in translated_ids:
            return False

        # CID paragraph check
        if is_cid_paragraph(paragraph):
            return False

        # Minimum length check
        if len(paragraph.unicode) < self.translation_config.min_text_length:
            return False

        # Body text check if requested
        if require_body_text and not self._is_body_text_paragraph(paragraph):
            return False

        return True

    def _filter_paragraphs(
        self,
        page: Page,
        translated_ids: set[int] | None = None,
        require_body_text: bool = False,
    ) -> list[PdfParagraph]:
        """Get list of paragraphs that should be translated from a page.

        Args:
            page: Page to get paragraphs from
            translated_ids: Set of already translated paragraph IDs
            require_body_text: Whether to filter for body text paragraphs only

        Returns:
            List of paragraphs that should be translated
        """
        return [
            paragraph
            for paragraph in page.pdf_paragraph
            if self._should_translate_paragraph(
                paragraph, translated_ids, require_body_text
            )
        ]

    def _build_font_maps(
        self, page: Page
    ) -> tuple[dict[str, PdfFont], dict[int, dict[str, PdfFont]]]:
        """Build font maps for a page.

        Args:
            page: The page to build font maps for

        Returns:
            Tuple of (page_font_map, page_xobj_font_map)
        """
        page_font_map = {}
        for font in page.pdf_font:
            page_font_map[font.font_id] = font

        page_xobj_font_map = {}
        for xobj in page.pdf_xobject:
            page_xobj_font_map[xobj.xobj_id] = page_font_map.copy()
            for font in xobj.pdf_font:
                page_xobj_font_map[xobj.xobj_id][font.font_id] = font

        return page_font_map, page_xobj_font_map

    def process_cross_page_paragraph(
        self,
        docs: Document,
        executor: PriorityThreadPoolExecutor,
        pbar: tqdm | None = None,
        tracker: DocumentTranslateTracker | None = None,
        executor2: PriorityThreadPoolExecutor | None = None,
        translated_ids: set[int] | None = None,
    ):
        """Process cross-page paragraphs by combining last body text paragraph of current page
        with first body text paragraph of next page.

        Args:
            docs: Document containing pages to process
            executor: Thread pool executor for translation tasks
            pbar: Progress bar for tracking translation progress
            tracker: Page translation tracker
            executor2: Secondary executor for fallback translation
            translated_ids: Set of already translated paragraph IDs
        """
        self.translation_config.raise_if_cancelled()

        if tracker is None:
            tracker = DocumentTranslateTracker()

        if translated_ids is None:
            translated_ids = set()

        # Process adjacent page pairs
        for i in range(len(docs.page) - 1):
            page_curr = docs.page[i]
            page_next = docs.page[i + 1]

            # Find body text paragraphs in current page
            curr_body_paragraphs = self._filter_paragraphs(
                page_curr, translated_ids, require_body_text=True
            )

            # Find body text paragraphs in next page
            next_body_paragraphs = self._filter_paragraphs(
                page_next, translated_ids, require_body_text=True
            )

            # Get last paragraph from current page and first paragraph from next page
            if not curr_body_paragraphs or not next_body_paragraphs:
                continue

            last_curr_paragraph = curr_body_paragraphs[-1]
            first_next_paragraph = next_body_paragraphs[0]

            # Skip if either paragraph is already translated
            if (
                id(last_curr_paragraph) in translated_ids
                or id(first_next_paragraph) in translated_ids
            ):
                continue

            # Build font maps for both pages
            curr_font_map, curr_xobj_font_map = self._build_font_maps(page_curr)
            next_font_map, next_xobj_font_map = self._build_font_maps(page_next)

            # Merge font maps
            merged_font_map = {**curr_font_map, **next_font_map}
            merged_xobj_font_map = {**curr_xobj_font_map, **next_xobj_font_map}

            # Calculate total token count
            total_token_count = self.calc_token_count(
                last_curr_paragraph.unicode
            ) + self.calc_token_count(first_next_paragraph.unicode)

            # Create batch with both paragraphs
            cross_page_paragraphs = [last_curr_paragraph, first_next_paragraph]
            cross_page_pages = [page_curr, page_next]
            batch_paragraph = BatchParagraph(
                cross_page_paragraphs, cross_page_pages, tracker.new_cross_page()
            )

            self.mid += 1
            # Submit translation task (force submit regardless of token count)
            executor.submit(
                self.translate_paragraph,
                batch_paragraph,
                pbar,
                merged_font_map,
                merged_xobj_font_map,
                self.translation_config.shared_context_cross_split_part.first_paragraph,
                self.translation_config.shared_context_cross_split_part.recent_title_paragraph,
                executor2,
                priority=1048576 - total_token_count,
                paragraph_token_count=total_token_count,
                mp_id=self.mid,
            )

            # Mark paragraphs as translated
            translated_ids.add(id(last_curr_paragraph))
            translated_ids.add(id(first_next_paragraph))

    def process_cross_column_paragraph(
        self,
        page: Page,
        executor: PriorityThreadPoolExecutor,
        pbar: tqdm | None = None,
        tracker: DocumentTranslateTracker | None = None,
        executor2: PriorityThreadPoolExecutor | None = None,
        translated_ids: set[int] | None = None,
    ):
        """Process cross-column paragraphs within the same page.

        If two adjacent body-text paragraphs have a gap in their y2 coordinate
        greater than 20 units, they are considered split across columns and
        will be translated together.
        """
        self.translation_config.raise_if_cancelled()

        if tracker is None:
            tracker = DocumentTranslateTracker()
        if translated_ids is None:
            translated_ids = set()

        # Filter body-text paragraphs maintaining original order
        body_paragraphs = self._filter_paragraphs(
            page, translated_ids, require_body_text=True
        )
        if len(body_paragraphs) < 2:
            return

        # Build font maps once for the whole page
        page_font_map, page_xobj_font_map = self._build_font_maps(page)

        for idx in range(len(body_paragraphs) - 1):
            p1 = body_paragraphs[idx]
            p2 = body_paragraphs[idx + 1]

            # Skip already translated
            if id(p1) in translated_ids or id(p2) in translated_ids:
                continue

            # Safety checks for box information
            if not (
                p1.box and p2.box and p1.box.y2 is not None and p2.box.y2 is not None
            ):
                continue

            if p2.box.y2 - p1.box.y2 <= 20:
                continue

            total_token_count = self.calc_token_count(
                p1.unicode
            ) + self.calc_token_count(p2.unicode)

            batch = BatchParagraph([p1, p2], [page, page], tracker.new_cross_column())
            self.mid += 1
            executor.submit(
                self.translate_paragraph,
                batch,
                pbar,
                page_font_map,
                page_xobj_font_map,
                self.translation_config.shared_context_cross_split_part.first_paragraph,
                self.translation_config.shared_context_cross_split_part.recent_title_paragraph,
                executor2,
                priority=1048576 - total_token_count,
                paragraph_token_count=total_token_count,
                mp_id=self.mid,
            )

            translated_ids.add(id(p1))
            translated_ids.add(id(p2))

    def process_page(
        self,
        page: Page,
        executor: PriorityThreadPoolExecutor,
        pbar: tqdm | None = None,
        tracker: PageTranslateTracker = None,
        executor2: PriorityThreadPoolExecutor | None = None,
        translated_ids: set | None = None,
    ):
        self.translation_config.raise_if_cancelled()
        page_font_map = {}
        for font in page.pdf_font:
            page_font_map[font.font_id] = font
        page_xobj_font_map = {}
        for xobj in page.pdf_xobject:
            page_xobj_font_map[xobj.xobj_id] = page_font_map.copy()
            for font in xobj.pdf_font:
                page_xobj_font_map[xobj.xobj_id][font.font_id] = font

        paragraphs = []

        total_token_count = 0
        for paragraph in page.pdf_paragraph:
            # Check if already translated
            if id(paragraph) in translated_ids:
                continue

            # Check basic validation
            if paragraph.debug_id is None or paragraph.unicode is None:
                continue

            # Check CID paragraph - advance progress bar if filtered out
            if is_cid_paragraph(paragraph):
                if pbar:
                    pbar.advance(1)
                continue

            # Check minimum length - advance progress bar if filtered out
            if len(paragraph.unicode) < self.translation_config.min_text_length:
                if pbar:
                    pbar.advance(1)
                continue

            if is_pure_numeric_paragraph(paragraph):
                if pbar:
                    pbar.advance(1)
                continue

            if is_placeholder_only_paragraph(paragraph):
                if pbar:
                    pbar.advance(1)
                continue

            # self.translate_paragraph(paragraph, pbar,tracker.new_paragraph(), page_font_map, page_xobj_font_map)
            total_token_count += self.calc_token_count(paragraph.unicode)
            paragraphs.append(paragraph)
            translated_ids.add(id(paragraph))
            if paragraph.layout_label == "title":
                self.shared_context_cross_split_part.recent_title_paragraph = (
                    self.shared_context_cross_split_part.snapshot_title_paragraph(
                        paragraph
                    )
                )

            if total_token_count > 200 or len(paragraphs) > 5:
                self.mid += 1
                executor.submit(
                    self.translate_paragraph,
                    BatchParagraph(paragraphs, [page] * len(paragraphs), tracker),
                    pbar,
                    page_font_map,
                    page_xobj_font_map,
                    self.translation_config.shared_context_cross_split_part.first_paragraph,
                    self.translation_config.shared_context_cross_split_part.recent_title_paragraph,
                    executor2,
                    priority=1048576 - total_token_count,
                    paragraph_token_count=total_token_count,
                    mp_id=self.mid,
                )
                paragraphs = []
                total_token_count = 0

        if paragraphs:
            self.mid += 1
            executor.submit(
                self.translate_paragraph,
                BatchParagraph(paragraphs, [page] * len(paragraphs), tracker),
                pbar,
                page_font_map,
                page_xobj_font_map,
                self.translation_config.shared_context_cross_split_part.first_paragraph,
                self.translation_config.shared_context_cross_split_part.recent_title_paragraph,
                executor2,
                priority=1048576 - total_token_count,
                paragraph_token_count=total_token_count,
                mp_id=self.mid,
            )

    def translate_paragraph(
        self,
        batch_paragraph: BatchParagraph,
        pbar: tqdm | None = None,
        page_font_map: dict[str, PdfFont] = None,
        xobj_font_map: dict[int, dict[str, PdfFont]] = None,
        title_paragraph: TitleContextSnapshot | None = None,
        local_title_paragraph: TitleContextSnapshot | None = None,
        executor: PriorityThreadPoolExecutor | None = None,
        paragraph_token_count: int = 0,
        mp_id: int = 0,
    ):
        """Translate a paragraph using pre and post processing functions."""
        self.translation_config.raise_if_cancelled()
        if self.il_translator.has_request_error:
            return
        should_translate_paragraph = []
        try:
            inputs = []
            llm_translate_trackers = []
            paragraph_unicodes = []
            for i in range(len(batch_paragraph.paragraphs)):
                paragraph = batch_paragraph.paragraphs[i]
                tracker = batch_paragraph.trackers[i]
                text, translate_input = self.il_translator.pre_translate_paragraph(
                    paragraph, tracker, page_font_map, xobj_font_map
                )
                if text is None:
                    pbar.advance(1)
                    continue

                tracker.record_multi_paragraph_id(mp_id)

                llm_translate_tracker = tracker.new_llm_translate_tracker()
                should_translate_paragraph.append(i)
                llm_translate_trackers.append(llm_translate_tracker)
                inputs.append(
                    (
                        text,
                        translate_input,
                        paragraph,
                        tracker,
                        llm_translate_tracker,
                        paragraph_unicodes,
                    )
                )
                paragraph_unicodes.append(paragraph.unicode)
            if not inputs:
                return
            json_format_input = []

            for id_, input_text in enumerate(inputs):
                ti: il_translator.ILTranslator.TranslateInput = input_text[1]
                tracker: ParagraphTranslateTracker = input_text[3]
                tracker.record_multi_paragraph_index(id_)
                placeholders_hint = ti.get_placeholders_hint()
                obj = {
                    "id": id_,
                    "input": input_text[0],
                    "layout_label": input_text[2].layout_label,
                }
                if (
                    placeholders_hint
                    and self.translation_config.add_formula_placehold_hint
                ):
                    obj["formula_placeholders_hint"] = placeholders_hint
                glosses = self._first_mention_glosses_of(input_text[2])
                if glosses:
                    obj[FIRST_MENTION_FIELD] = glosses
                heading = self._key_concept_heading_of(input_text[2])
                if heading:
                    obj[KEY_CONCEPT_HEADING_FIELD] = heading
                json_format_input.append(obj)

            json_format_input_str = json.dumps(
                json_format_input, ensure_ascii=False, indent=2
            )

            batch_text_for_glossary_matching = "\n".join(
                item.get("input", "") for item in json_format_input
            )

            final_input = self._build_llm_prompt(
                json_input_str=json_format_input_str,
                title_paragraph=title_paragraph,
                local_title_paragraph=local_title_paragraph,
                batch_text_for_glossary_matching=batch_text_for_glossary_matching,
                paper_context_block=self._paper_context_block(
                    batch_paragraph.paragraphs
                ),
                has_first_mention_glosses=any(
                    FIRST_MENTION_FIELD in obj for obj in json_format_input
                ),
                has_key_concept_headings=any(
                    KEY_CONCEPT_HEADING_FIELD in obj for obj in json_format_input
                ),
            )

            for llm_translate_tracker in llm_translate_trackers:
                llm_translate_tracker.set_input(final_input)
            # The translator retries an answer that does not parse like a
            # failed request (validate_output raises on it). page_number
            # (1-based) tells it where the batch starts.
            first_page = batch_paragraph.pages[should_translate_paragraph[0]]
            llm_output = self.translate_engine.llm_translate(
                final_input,
                rate_limit_params={
                    "paragraph_token_count": paragraph_token_count,
                    "page_number": None
                    if first_page.page_number is None
                    else first_page.page_number + 1,
                    "request_json_mode": True,
                    "validate_output": lambda output: self._parse_llm_output(
                        output, len(inputs)
                    ),
                },
            )
            for llm_translate_tracker in llm_translate_trackers:
                llm_translate_tracker.set_output(llm_output)
            translation_results = self._parse_llm_output(llm_output, len(inputs))
            review_kept: set = set()
            if self.translation_config.batch_reviewer is not None:
                if self.il_translator.has_request_error:
                    return
                translation_results, review_kept = self._review(
                    translation_results,
                    inputs,
                    json_format_input,
                    batch_paragraph,
                    should_translate_paragraph,
                    batch_text_for_glossary_matching,
                )

            for id_, output in translation_results.items():
                should_fallback = True
                if output is None and id_ in review_kept:
                    # The review kept this paragraph's source text and
                    # recorded it. Any other null is a null draft: it is
                    # redone alone below (M4 ticket 15).
                    should_fallback = False
                    self.total_count += 1
                    if pbar:
                        pbar.advance(1)
                    continue
                try:
                    if not isinstance(output, str):
                        logger.warning(
                            f"Translation result is not a string. Output: {output}"
                        )
                        continue

                    id_ = int(id_)  # Ensure id is an integer
                    if id_ >= len(inputs):
                        logger.warning(f"Invalid id {id_}, skipping")
                        continue

                    # Clean up any excessive punctuation in the translated text
                    translated_text = re.sub(r"[. 。…，]{20,}", ".", output)

                    # A blank answer would clear the paragraph, and a short
                    # input's blank answer can pass the length check: it
                    # fails here and is redone alone (M4 ticket 15).
                    if not translated_text.strip():
                        inputs[id_][4].set_error_message("Translation result is empty.")
                        logger.warning("Translation result is empty, fallback.")
                        inputs[id_][4].set_placeholder_full_match()
                        continue

                    # Get the original input for this translation
                    translate_input = inputs[id_][1]
                    llm_translate_tracker = inputs[id_][4]

                    input_unicode = inputs[id_][0]
                    output_unicode = translated_text

                    trimed_input = re.sub(r"[. 。…，]{20,}", ".", input_unicode)

                    # A requested gloss lengthens the output; it must not
                    # make the length check reject the translation.
                    glosses = self._first_mention_glosses_of(inputs[id_][2])
                    input_token_count = self.calc_token_count(
                        trimed_input + "".join(g["gloss_zh"] for g in glosses)
                    )
                    output_token_count = self.calc_token_count(output_unicode)

                    same_as_input = trimed_input == output_unicode
                    if (
                        same_as_input
                        and input_token_count > 10
                        and not self.translation_config.disable_same_text_fallback
                    ):
                        llm_translate_tracker.set_error_message(
                            "Translation result is the same as input, fallback."
                        )
                        llm_translate_tracker.set_placeholder_full_match()
                        logger.warning(
                            "Translation result is the same as input, fallback."
                        )
                        continue

                    if not translation_length_ok(
                        trimed_input
                        + "".join(g["gloss_zh"] for g in glosses),
                        output_unicode,
                        input_token_count,
                        output_token_count,
                    ):
                        llm_translate_tracker.set_error_message(
                            f"Translation result is too long or too short. Input: {input_token_count}, Output: {output_token_count}"
                        )
                        logger.warning(
                            f"Translation result is too long or too short. Input: {input_token_count}, Output: {output_token_count}"
                        )
                        llm_translate_tracker.set_placeholder_full_match()
                        continue

                    if not self.translation_config.disable_same_text_fallback:
                        edit_distance = Levenshtein.distance(
                            input_unicode, output_unicode
                        )
                        if edit_distance < 5 and input_token_count > 20:
                            llm_translate_tracker.set_error_message(
                                f"Translation result edit distance is too small. distance: {edit_distance}, input: {input_unicode}, output: {output_unicode}"
                            )
                            logger.warning(
                                f"Translation result edit distance is too small. distance: {edit_distance}, input: {input_unicode}, output: {output_unicode}"
                            )
                            llm_translate_tracker.set_placeholder_full_match()
                            continue
                    # Apply the translation to the paragraph
                    self.il_translator.post_translate_paragraph(
                        inputs[id_][2],
                        inputs[id_][3],
                        translate_input,
                        translated_text,
                    )
                    should_fallback = False
                    if pbar:
                        pbar.advance(1)
                except Exception as e:
                    error_message = f"Error translating paragraph. Error: {e}."
                    logger.exception(error_message)
                    # Ignore error and continue
                    for llm_translate_tracker in llm_translate_trackers:
                        llm_translate_tracker.set_error_message(error_message)
                    continue
                finally:
                    self.total_count += 1
                    if should_fallback:
                        self.fallback_count += 1
                        inputs[id_][4].set_fallback_to_translate()
                        logger.warning(
                            f"Fallback to simple translation. paragraph id: {inputs[id_][2].debug_id}"
                        )
                        paragraph_token_count = self.calc_token_count(
                            inputs[id_][2].unicode
                        )
                        paragraph_unicodes = inputs[id_][5]
                        inputs[id_][2].unicode = paragraph_unicodes[id_]
                        executor.submit(
                            self.il_translator.translate_paragraph,
                            inputs[id_][2],
                            batch_paragraph.pages[id_],
                            pbar,
                            inputs[id_][3],
                            page_font_map,
                            xobj_font_map,
                            priority=1048576 - paragraph_token_count,
                            paragraph_token_count=paragraph_token_count,
                            title_paragraph=title_paragraph,
                            local_title_paragraph=local_title_paragraph,
                        )
                    else:
                        self.ok_count += 1

        except TranslationRequestError as e:
            for llm_translate_tracker in llm_translate_trackers:
                llm_translate_tracker.set_error_message(f"Request failed for good: {e}")
            first = should_translate_paragraph[0] if should_translate_paragraph else 0
            self.il_translator.record_request_error(e, batch_paragraph.pages[first])
        except Exception as e:
            error_message = f"Error {e} during translation. try fallback"
            logger.warning(error_message)
            for llm_translate_tracker in llm_translate_trackers:
                llm_translate_tracker.set_error_message(error_message)
                llm_translate_tracker.set_fallback_to_translate()
            self.total_count += len(llm_translate_trackers)
            self.fallback_count += len(llm_translate_trackers)
            for input_ in inputs:
                input_[2].unicode = input_[5]
            if not should_translate_paragraph:
                should_translate_paragraph = list(
                    range(len(batch_paragraph.paragraphs))
                )
            for i in should_translate_paragraph:
                paragraph = batch_paragraph.paragraphs[i]
                tracker = batch_paragraph.trackers[i]
                if paragraph.debug_id is None:
                    continue
                paragraph_token_count = self.calc_token_count(paragraph.unicode)
                executor.submit(
                    self.il_translator.translate_paragraph,
                    paragraph,
                    batch_paragraph.pages[i],
                    pbar,
                    tracker,
                    page_font_map,
                    xobj_font_map,
                    priority=1048576 - paragraph_token_count,
                    paragraph_token_count=paragraph_token_count,
                    title_paragraph=title_paragraph,
                    local_title_paragraph=local_title_paragraph,
                )

    def _review(
        self,
        translation_results: dict,
        inputs: list,
        json_format_input: list[dict],
        batch_paragraph: BatchParagraph,
        should_translate_paragraph: list[int],
        batch_text: str,
    ) -> tuple[dict, set]:
        """Review the batch's drafts.

        Return {id: final text, or None to keep the source} and the ids whose
        source the review kept (and recorded). Drafts that are not strings,
        null included, or are blank, are left to the checks below, which
        redo them one by one (M4 ticket 15).
        """
        items = []
        for obj in json_format_input:
            id_ = obj["id"]
            draft = translation_results.get(id_)
            if not isinstance(draft, str) or not draft.strip():
                continue
            page = batch_paragraph.pages[should_translate_paragraph[id_]]
            item = {
                "id": id_,
                "source": inputs[id_][0],
                "draft": draft,
                "page": page.page_number + 1,
            }
            for field in (FIRST_MENTION_FIELD, KEY_CONCEPT_HEADING_FIELD):
                if field in obj:
                    item[field] = obj[field]
            items.append(item)
        if not items:
            return translation_results, set()
        final = self.translation_config.batch_reviewer(
            items, self._active_glossary_entries(batch_text)
        )
        kept = {id_ for id_, text in final.items() if text is None}
        return {**translation_results, **final}, kept

    def _active_glossary_entries(self, text: str) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for glossary in self._cached_glossaries or []:
            entries.extend(glossary.get_active_entries_for_text(text))
        return sorted(set(entries))

    def _build_llm_prompt(
        self,
        json_input_str: str,
        title_paragraph: TitleContextSnapshot | None,
        local_title_paragraph: TitleContextSnapshot | None,
        batch_text_for_glossary_matching: str,
        paper_context_block: str | None = None,
        has_first_mention_glosses: bool = False,
        has_key_concept_headings: bool = False,
    ) -> str:
        """Build LLM prompt using a single template for easier maintenance.

        A ``paper_context_block`` replaces the upstream title hints: it holds
        the batch's section title along with the dossier and neighbours.
        """
        # Build role block, honoring custom_system_prompt if provided.
        custom_prompt = getattr(self.translation_config, "custom_system_prompt", None)
        if custom_prompt:
            role_block = custom_prompt.strip()
            if "Follow all rules strictly." not in role_block:
                if not role_block.endswith("\n"):
                    role_block += "\n"
                role_block += "Follow all rules strictly."
        else:
            role_block = (
                f"You are a professional {self.translation_config.lang_out} native translator who needs to fluently translate text "
                f"into {self.translation_config.lang_out}.\n\n"
                "Follow all rules strictly."
            )

        # Build contextual hints section.
        contextual_lines: list[str] = []
        hint_idx = 1
        if title_paragraph:
            contextual_lines.append(
                f"{hint_idx}. First title in full text: {title_paragraph.unicode}"
            )
            hint_idx += 1

        if local_title_paragraph:
            is_different_from_global = True
            if title_paragraph:
                if local_title_paragraph.debug_id == title_paragraph.debug_id:
                    is_different_from_global = False

            if is_different_from_global:
                contextual_lines.append(
                    f"{hint_idx}. The most recent title is: {local_title_paragraph.unicode}"
                )

        if paper_context_block is not None:
            contextual_hints_block = paper_context_block + "\n"
        elif contextual_lines:
            contextual_hints_block = (
                "## Contextual Hints for Better Translation\n"
                + "\n".join(contextual_lines)
                + "\n"
            )
        else:
            contextual_hints_block = ""

        # Build glossary usage rules and glossary tables.
        glossary_usage_rules_block = ""
        glossary_tables_block = ""
        glossary_entries_per_glossary: dict[str, list[tuple[str, str]]] = {}

        if self._cached_glossaries:
            for glossary in self._cached_glossaries:
                active_entries = glossary.get_active_entries_for_text(
                    batch_text_for_glossary_matching
                )
                if active_entries:
                    glossary_entries_per_glossary[glossary.name] = sorted(
                        active_entries
                    )

        if glossary_entries_per_glossary:
            glossary_usage_rules_block = (
                "## Glossary\n"
                "If a glossary is provided:\n"
                "- Always use the exact target term.\n"
                "- A target term identical to its source term is a key concept of this paper: "
                "keep it in English exactly as written, never translate it.\n"
                "- Apply glossary items even inside tags or when broken by hyphens/line breaks.\n"
                "- If glossary does NOT include a term, translate it naturally.\n\n"
            )

            glossary_table_lines: list[str] = ["## Glossary Tables", ""]
            for glossary_name, entries in glossary_entries_per_glossary.items():
                glossary_table_lines.append(f"### Glossary: {glossary_name}")
                glossary_table_lines.append("")
                glossary_table_lines.append(
                    "| Source Term | Target Term |\n|-------------|-------------|"
                )
                for original_source, target_text in entries:
                    glossary_table_lines.append(
                        f"| {original_source} | {target_text} |"
                    )
                glossary_table_lines.append("")
            glossary_tables_block = "\n".join(glossary_table_lines)

        return PROMPT_TEMPLATE.substitute(
            role_block=role_block,
            glossary_usage_rules_block=glossary_usage_rules_block,
            first_mention_rules_block=FIRST_MENTION_RULES
            if has_first_mention_glosses
            else "",
            key_concept_heading_rules_block=KEY_CONCEPT_HEADING_TRANSLATION_RULES
            if has_key_concept_headings
            else "",
            # A user model profile's rules (M4 ticket 15); none for the
            # built-in profile, whose prompt stays as it was.
            batch_extra_rules_block=getattr(
                self.translation_config, "batch_extra_rules", None
            )
            or "",
            contextual_hints_block=contextual_hints_block,
            json_input_str=json_input_str,
            glossary_tables_block=glossary_tables_block,
            lang_out=self.translation_config.lang_out,
            no_added_parentheses_rules=NO_ADDED_PARENTHESES_RULES,
        )

    def _parse_llm_output(self, llm_output: str, expected_count: int) -> dict:
        """Return ``{id: output}`` from a batch answer; raise if it does not parse."""
        parsed_output = json.loads(self._clean_json_output(llm_output.strip()))

        if isinstance(parsed_output, dict) and parsed_output.get(
            "output", parsed_output.get("input", False)
        ):
            parsed_output = [parsed_output]

        translation_results = {
            item["id"]: item.get("output", item.get("input")) for item in parsed_output
        }

        if len(translation_results) != expected_count:
            raise Exception(
                f"Translation results length mismatch. Expected: {expected_count}, Got: {len(translation_results)}"
            )
        return translation_results

    def _clean_json_output(self, llm_output: str) -> str:
        # Clean up JSON output by removing common wrapper tags
        llm_output = llm_output.strip()
        if llm_output.startswith("<json>"):
            llm_output = llm_output[6:]
        if llm_output.endswith("</json>"):
            llm_output = llm_output[:-7]
        if llm_output.startswith("```json"):
            llm_output = llm_output[7:]
        if llm_output.startswith("```"):
            llm_output = llm_output[3:]
        if llm_output.endswith("```"):
            llm_output = llm_output[:-3]
        return llm_output.strip()
