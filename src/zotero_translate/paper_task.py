"""Paper task: translate one PDF into a mono PDF and a dual PDF.

Both the translation service's subprocess and the development command call
``run_paper_task``. The engine is imported lazily so that importing this
module (for the stage names) stays cheap.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from zotero_translate.settings import PipelineSettings
from zotero_translate.task_dir import DOSSIER_FILE
from zotero_translate.task_dir import OUTPUT_FILES
from zotero_translate.task_dir import REPORT_FILE
from zotero_translate.task_dir import TERMS_FILE

READ_STAGE = "读取全文"
DOSSIER_STAGE = "生成论文档案"
TERMS_STAGE = "生成本篇术语清单"
PARSE_STAGE = "解析版面"
TRANSLATE_STAGE = "翻译与审校"
TYPESET_STAGE = "排版"
FIRST_STAGE = READ_STAGE
_STAGE_ORDER = [PARSE_STAGE, TRANSLATE_STAGE, TYPESET_STAGE]
# Overall progress: reading, the dossier and the term list take 0-4 %,
# the engine 5-99 %.
_DOSSIER_PROGRESS = 2
_TERMS_PROGRESS = 3
_ENGINE_START = 5

# Engine stage name -> our stage name. Engine stages not listed here happen
# before translation and count as layout parsing.
_ENGINE_STAGES = {
    "Translate Paragraphs": TRANSLATE_STAGE,
    "Typesetting": TYPESET_STAGE,
    "Add Fonts": TYPESET_STAGE,
    "Generate drawing instructions": TYPESET_STAGE,
    "Subset font": TYPESET_STAGE,
    "Save PDF": TYPESET_STAGE,
}

ProgressCallback = Callable[[str, int], None]


class PaperTaskError(Exception):
    """The paper task failed; the message is a readable Chinese reason.

    ``category`` is the failure category (spec section 1), carried from the
    model call error that caused it: ``permanent`` for a credential error,
    which a rerun cannot fix; ``retryable`` otherwise.
    """

    def __init__(
        self,
        message: str,
        failed_batches: list[dict] | None = None,
        category: str = "retryable",
    ):
        super().__init__(message)
        self.failed_batches = failed_batches or []
        self.category = category


@dataclass
class PaperTaskResult:
    mono_pdf: Path
    dual_pdf: Path
    model_requests: int  # both endpoints
    max_in_flight: int  # flash
    argon_max_in_flight: int
    forwarded_requests: int  # from flash to argon
    kept_source_paragraphs: int
    kept_source_algorithms: int


def run_paper_task(
    source_pdf: Path,
    output_dir: Path,
    settings: PipelineSettings,
    on_progress: ProgressCallback = lambda stage, progress: None,
    report_path: Path | None = None,
    attempt: int | None = None,
) -> PaperTaskResult:
    """Translate ``source_pdf``; write ``mono.pdf`` and ``dual.pdf`` into ``output_dir``.

    The paper dossier and the term list requests go to argon, every other
    request to flash (ADR 0004).

    ``on_progress(stage, progress)`` receives our stage name and an overall
    percentage (0-99) that never decreases. The paper dossier and the
    paper's term list are written to ``output_dir`` before translation; the
    task report is written to ``report_path`` (default
    ``output_dir/report.json``) whether the task succeeds or fails. The
    service passes the attempt number, which the report then carries.
    """
    from babeldoc.docvision.doclayout import DocLayoutModel
    from babeldoc.format.pdf import high_level
    from babeldoc.format.pdf.translation_config import TranslationConfig
    from babeldoc.format.pdf.translation_config import WatermarkOutputMode
    from babeldoc.glossary import Glossary
    from babeldoc.glossary import GlossaryEntry
    from babeldoc.translator.translator import set_translate_rate_limiter

    from zotero_translate.algorithm_layout import AlgorithmLayout
    from zotero_translate.algorithm_translation import AlgorithmTranslator
    from zotero_translate.algorithms import AlgorithmDetector
    from zotero_translate.batch_context import BatchContext
    from zotero_translate.dossier import read_full_text
    from zotero_translate.dossier import request_dossier
    from zotero_translate.first_mention import FirstMentionPlan
    from zotero_translate.first_mention import front_matter_range
    from zotero_translate.first_mention import references_range
    from zotero_translate.lost_text import report_lost_text
    from zotero_translate.model_client import ModelCallError
    from zotero_translate.model_client import PipelineModels
    from zotero_translate.review import BatchReviewer
    from zotero_translate.review import KeptSource
    from zotero_translate.task_report import TaskReport
    from zotero_translate.terms import GLOSSARY_NAME
    from zotero_translate.terms import request_term_list
    from zotero_translate.translator import PipelineTranslator

    output_dir = Path(output_dir)
    engine_output = output_dir / "engine-output"
    models = PipelineModels(settings)
    client = models.flash
    kept = KeptSource()
    algorithms = AlgorithmDetector()
    report = TaskReport(models, kept, algorithms, attempt)

    def progress(stage: str, percent: int) -> None:
        report.enter_stage(stage)
        on_progress(stage, percent)

    try:
        progress(READ_STAGE, 0)
        full_text = read_full_text(source_pdf)
        progress(DOSSIER_STAGE, _DOSSIER_PROGRESS)
        try:
            dossier = request_dossier(models.argon, full_text)
        except ModelCallError as e:
            raise PaperTaskError(f"生成论文档案失败：{e}", category=e.category) from e
        (output_dir / DOSSIER_FILE).write_text(
            json.dumps(dossier, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        progress(TERMS_STAGE, _TERMS_PROGRESS)
        try:
            term_list = request_term_list(models.argon, full_text, dossier)
        except ModelCallError as e:
            raise PaperTaskError(f"生成本篇术语清单失败：{e}", category=e.category) from e
        report.terms = term_list.report()
        (output_dir / TERMS_FILE).write_text(
            json.dumps(term_list.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        glossary = Glossary(
            GLOSSARY_NAME,
            [GlossaryEntry(source, target, "zh") for source, target in term_list.glossary_entries()],
        )
        first_mentions = FirstMentionPlan(term_list.terms)
        report.first_mentions = first_mentions.placements
        reviewer = BatchReviewer(client, dossier, kept)
        algorithm_translator = AlgorithmTranslator(
            client, dossier, algorithms, term_list.glossary_entries()
        )
        report.reviewer = reviewer

        high_level.init()
        # The engine's thread pools and rate limiter must not hold requests
        # back below the pipeline model client's own concurrency limit.
        set_translate_rate_limiter(client.settings.concurrency)
        doc_layout_model = DocLayoutModel.load_onnx()
        config = TranslationConfig(
            translator=PipelineTranslator(client),
            input_file=str(source_pdf),
            lang_in="en",
            lang_out="zh",
            doc_layout_model=doc_layout_model,
            output_dir=str(engine_output),
            qps=client.settings.concurrency,
            pool_max_workers=client.settings.concurrency,
            watermark_output_mode=WatermarkOutputMode.NoWatermark,
            auto_extract_glossary=False,
            glossaries=[glossary],
            disable_same_text_fallback=True,
            batch_context_builder=BatchContext(dossier),
            first_mention_planner=first_mentions,
            first_mention_recorder=first_mentions.record_translations,
            key_concept_headings=first_mentions.key_concept_headings,
            batch_reviewer=reviewer,
            on_source_kept=kept.record,
            algorithm_detector=algorithms.engine_page,
            algorithm_translator=algorithm_translator.start,
            algorithm_typesetter=AlgorithmLayout(algorithms, algorithm_translator),
            reference_section=references_range,
            front_matter=front_matter_range,
            use_rich_pbar=False,
        )
        getattr(doc_layout_model, "init_font_mapper", lambda _config: None)(config)
        result = asyncio.run(_drive_engine(high_level, config, progress))
        report.first_mentions = first_mentions.placements
        report.first_mention_glosses = first_mentions.written
        mono = output_dir / OUTPUT_FILES["mono"]
        dual = output_dir / OUTPUT_FILES["dual"]
        if not result.mono_pdf_path or not result.dual_pdf_path:
            raise PaperTaskError("引擎没有生成单语译文 PDF 或双语对照 PDF")
        shutil.move(result.mono_pdf_path, mono)
        shutil.move(result.dual_pdf_path, dual)
        shutil.rmtree(engine_output, ignore_errors=True)
        report.lost_text = report_lost_text(source_pdf, mono)
        return PaperTaskResult(
            mono_pdf=mono,
            dual_pdf=dual,
            model_requests=client.request_count + models.argon.request_count,
            max_in_flight=client.max_in_flight,
            argon_max_in_flight=models.argon.max_in_flight,
            forwarded_requests=len(client.forwarded_pages),
            kept_source_paragraphs=len(kept.paragraphs),
            kept_source_algorithms=algorithms.kept_source,
        )
    except PaperTaskError as e:
        report.fail(str(e), e.failed_batches, e.category)
        raise
    except BaseException as e:
        report.fail(f"论文任务出错：{type(e).__name__}: {e}")
        raise
    finally:
        report.write(report_path or output_dir / REPORT_FILE)
        models.close()


async def _drive_engine(high_level, config, on_progress: ProgressCallback):
    from babeldoc.babeldoc_exception.BabelDOCException import TranslationRequestError
    from babeldoc.babeldoc_exception.BabelDOCException import (
        TranslationRequestsFailedError,
    )

    progress = _ENGINE_START
    stage = PARSE_STAGE
    on_progress(stage, progress)
    async for event in high_level.async_translate(config):
        if event["type"] == "error":
            error = event["error"]
            if isinstance(error, TranslationRequestsFailedError):
                # Any credential error among the failures makes the whole
                # attempt permanent, and its message is the reason given.
                cause = error.cause
                where = f"第 {cause.page_number} 页附近" if cause.page_number else "页码未知"
                raise PaperTaskError(
                    f"管线模型请求失败（{where}）：{cause.message}",
                    failed_batches=[
                        {"page": f.page_number, "error": f.message, "category": f.category}
                        for f in error.failures
                    ],
                    category=error.category,
                )
            category = error.category if isinstance(error, TranslationRequestError) else "retryable"
            raise PaperTaskError(f"引擎运行出错：{error}", category=category)
        if event["type"] == "finish":
            return event["translate_result"]
        new_stage = max(
            stage, _ENGINE_STAGES.get(event.get("stage"), PARSE_STAGE), key=_STAGE_ORDER.index
        )
        engine_progress = float(event.get("overall_progress") or 0)
        scaled = _ENGINE_START + int(engine_progress * (99 - _ENGINE_START) / 100)
        new_progress = max(progress, min(99, scaled))
        if (new_stage, new_progress) != (stage, progress):
            stage, progress = new_stage, new_progress
            on_progress(stage, progress)
    raise PaperTaskError("引擎没有报告完成")
