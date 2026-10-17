"""Paper task: translate one PDF into a mono PDF and a dual PDF.

Both the translation service's subprocess and the development command call
``run_paper_task``. The engine is imported lazily so that importing this
module (for the stage names) stays cheap.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from zotero_translate.settings import ModelSettings
from zotero_translate.task_dir import OUTPUT_FILES
from zotero_translate.task_dir import REPORT_FILE

PARSE_STAGE = "解析版面"
TRANSLATE_STAGE = "翻译与审校"
TYPESET_STAGE = "排版"
FIRST_STAGE = PARSE_STAGE
_STAGE_ORDER = [PARSE_STAGE, TRANSLATE_STAGE, TYPESET_STAGE]

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
    """The paper task failed; the message is a readable Chinese reason."""

    def __init__(self, message: str, failed_batches: list[dict] | None = None):
        super().__init__(message)
        self.failed_batches = failed_batches or []


@dataclass
class PaperTaskResult:
    mono_pdf: Path
    dual_pdf: Path
    model_requests: int
    max_in_flight: int


def run_paper_task(
    source_pdf: Path,
    output_dir: Path,
    settings: ModelSettings,
    on_progress: ProgressCallback = lambda stage, progress: None,
) -> PaperTaskResult:
    """Translate ``source_pdf``; write ``mono.pdf`` and ``dual.pdf`` into ``output_dir``.

    ``on_progress(stage, progress)`` receives our stage name and an overall
    percentage (0-99) that never decreases. The task report is written to
    ``output_dir`` whether the task succeeds or fails.
    """
    from babeldoc.docvision.doclayout import DocLayoutModel
    from babeldoc.format.pdf import high_level
    from babeldoc.format.pdf.translation_config import TranslationConfig
    from babeldoc.format.pdf.translation_config import WatermarkOutputMode
    from babeldoc.translator.translator import set_translate_rate_limiter

    from zotero_translate.model_client import ModelClient
    from zotero_translate.task_report import TaskReport
    from zotero_translate.translator import PipelineTranslator

    output_dir = Path(output_dir)
    engine_output = output_dir / "engine-output"
    client = ModelClient(settings)
    report = TaskReport()

    def progress(stage: str, percent: int) -> None:
        report.enter_stage(stage)
        on_progress(stage, percent)

    try:
        high_level.init()
        # The engine's thread pools and rate limiter must not hold requests
        # back below the pipeline model client's own concurrency limit.
        set_translate_rate_limiter(settings.concurrency)
        doc_layout_model = DocLayoutModel.load_onnx()
        config = TranslationConfig(
            translator=PipelineTranslator(client),
            input_file=str(source_pdf),
            lang_in="en",
            lang_out="zh",
            doc_layout_model=doc_layout_model,
            output_dir=str(engine_output),
            qps=settings.concurrency,
            pool_max_workers=settings.concurrency,
            watermark_output_mode=WatermarkOutputMode.NoWatermark,
            auto_extract_glossary=False,
            disable_same_text_fallback=True,
            use_rich_pbar=False,
        )
        getattr(doc_layout_model, "init_font_mapper", lambda _config: None)(config)
        result = asyncio.run(_drive_engine(high_level, config, progress))
        mono = output_dir / OUTPUT_FILES["mono"]
        dual = output_dir / OUTPUT_FILES["dual"]
        if not result.mono_pdf_path or not result.dual_pdf_path:
            raise PaperTaskError("引擎没有生成单语译文 PDF 或双语对照 PDF")
        shutil.move(result.mono_pdf_path, mono)
        shutil.move(result.dual_pdf_path, dual)
        shutil.rmtree(engine_output, ignore_errors=True)
        return PaperTaskResult(
            mono_pdf=mono,
            dual_pdf=dual,
            model_requests=client.request_count,
            max_in_flight=client.max_in_flight,
        )
    except PaperTaskError as e:
        report.fail(str(e), e.failed_batches)
        raise
    except BaseException as e:
        report.fail(f"论文任务出错：{type(e).__name__}: {e}")
        raise
    finally:
        report.write(output_dir / REPORT_FILE, client)
        client.close()


async def _drive_engine(high_level, config, on_progress: ProgressCallback):
    from babeldoc.babeldoc_exception.BabelDOCException import (
        TranslationRequestsFailedError,
    )

    progress = 0
    stage = FIRST_STAGE
    on_progress(stage, progress)
    async for event in high_level.async_translate(config):
        if event["type"] == "error":
            error = event["error"]
            if isinstance(error, TranslationRequestsFailedError):
                first = error.failures[0]
                where = f"第 {first.page_number} 页附近" if first.page_number else "页码未知"
                raise PaperTaskError(
                    f"管线模型请求失败（{where}）：{first.message}",
                    failed_batches=[
                        {"page": f.page_number, "error": f.message} for f in error.failures
                    ],
                )
            raise PaperTaskError(f"引擎运行出错：{error}")
        if event["type"] == "finish":
            return event["translate_result"]
        new_stage = max(
            stage, _ENGINE_STAGES.get(event.get("stage"), PARSE_STAGE), key=_STAGE_ORDER.index
        )
        new_progress = max(progress, min(99, int(event.get("overall_progress") or 0)))
        if (new_stage, new_progress) != (stage, progress):
            stage, progress = new_stage, new_progress
            on_progress(stage, progress)
    raise PaperTaskError("引擎没有报告完成")
