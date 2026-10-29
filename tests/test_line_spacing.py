"""Line spacing and scale floor of translated paragraphs (closure ticket 03, F1).

A Chinese paragraph was set with a line skip of 1.5 times the font size, so
the abstract of AutoRef page 1 (source 10.1 pt on an 11.0 pt line pitch) only
fit its box at 9.0 pt on a 13.5 pt pitch: the engine shrank the font because
the lines were far apart, not because the text was too long.
"""

import collections
import json
import logging
import re
from types import SimpleNamespace

import pymupdf
import pytest
from babeldoc.format.pdf.document_il import Box, PdfCharacter, PdfStyle
from babeldoc.format.pdf.document_il.midend import typesetting as typesetting_module
from babeldoc.format.pdf.document_il.midend.typesetting import (
    MIN_SCALE,
    Typesetting,
    TypesettingUnit,
)

from tests.conftest import FIXTURES
from tests.fake_model import PSEUDO_PREFIX, batch_items

STYLE_SPAN = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL)
PLACEHOLDER = re.compile(r"\{v\d+\}")
CJK = re.compile(r"[一-鿿]")
ABSTRACT_START = "Recent image generation models can take multiple reference images"
URL = "https://github.com/KuOnoda/AutoRef"
# The real model's translation of the abstract (closure-02 run, AutoRef mono page 1).
ABSTRACT_ZH = (
    "近期的图像生成模型能够以多张参考图像作为输入，并将它们组合成一幅新图像。"
    "然而，multi-reference（基于多张参考图的生成设定）图像生成仍具挑战：模型"
    "可能会遗漏或重复参考图中的主体，或者生成的图像中多个主体显得生硬拼贴。近"
    "期的研究提出了结合图像生成模型、推理模型以及 harness（调度冻结模型的可执"
    "行程序）的图像生成智能体，harness 是一个可执行程序，规定了如何解析参考图"
    "像、如何执行生成、如何诊断输出以及如何选择最终图像。然而，在 "
    "multi-reference 生成中，参考图像扮演着不同的角色，且输出必须同时满足多项"
    "标准，例如对每张参考图的保真度以及整幅图像的自然度，因此从参考图的处理方"
    "式到输出的诊断方式，harness 的诸多部分均可加以改进。这使得人们难以预测哪"
    "些修改能够提升性能以及能提升多少，也让优秀的 harnesses 难以手工设计；事实"
    "上，人工编写的 harnesses 在性能上差异巨大。我们因此提出了 AutoRef（自动优"
    "化 harness 代码的方法），它在保持这两个模型冻结的同时自动优化 harness：由"
    "编码智能体迭代重写 harness 代码。AutoRef 将提供反馈以启发提案的任务与用"
    "于筛选候选的任务分离开来，并从筛选任务上排名靠前的 harnesses 束继续展开搜"
    "索。利用这一流程，我们发现了 AutoRef-Harness（AutoRef 搜索出的图像生成程"
    "序），它将开源权重 FLUX.2 [klein] 4B 在 MultiBanana 基准的留出四参考任务上"
    "的得分从 5.72 提升至 7.37（满分 10 分），达到或超过了包括 Nano Banana "
    "Pro 和 GPT-Image-1.5 在内的专有模型。无需重新优化，当生成器、参考图像数"
    "量、基准测试、评估器或推理模型与搜索中所用的不同时，该 harness 同样能提升"
    "结果。我们将代码与 AutoRef-Harness 开源在 "
)


def abstract_in_chinese(body: dict) -> str:
    """The abstract as the real model translated it, with the formula placeholders
    of "FLUX.2 [klein] 4B" and the link span of the input; every other paragraph
    pseudo-translated."""
    answer = []
    for item in batch_items(body["messages"][-1]["content"]) or []:
        text = item["input"]
        if ABSTRACT_START in text:
            left, right = PLACEHOLDER.findall(text)
            link = next(s for s in STYLE_SPAN.findall(text) if URL in s)
            flux = f"FLUX.{left}klein{right}B"
            output = ABSTRACT_ZH.replace("FLUX.2 [klein] 4B", flux) + link + "。"
        else:
            output = PSEUDO_PREFIX + text
        answer.append({"id": item["id"], "output": output})
    return json.dumps(answer, ensure_ascii=False)


def abstract_box(source: bytes) -> pymupdf.Rect:
    """The abstract box on page 1 (the scorecard's "abstract" rule: the largest
    source text block)."""
    with pymupdf.open(stream=source, filetype="pdf") as src:
        blocks = [b for b in src[0].get_text("blocks") if b[6] == 0]
        return pymupdf.Rect(max(blocks, key=lambda b: len(b[4]))[:4])


def abstract_cjk_sizes(source: bytes, mono: bytes) -> collections.Counter:
    """Font sizes of the CJK characters inside the source abstract box on page 1."""
    box = abstract_box(source)
    sizes = collections.Counter()
    with pymupdf.open(stream=mono, filetype="pdf") as doc:
        page = doc[0].get_text("dict", clip=box)
        for block in page["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    n = len(CJK.findall(span["text"]))
                    if n:
                        sizes[round(span["size"], 1)] += n
    return sizes


def test_the_autoref_abstract_keeps_a_font_size_close_to_the_source(fake_model, start_service):
    fake_model.respond("translate_batch", abstract_in_chinese)
    service = start_service(fake_model)

    pdf = FIXTURES / "autoref-p1.pdf"
    task_id = service.submit(pdf)
    final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    mono = service.download(task_id, "mono")
    sizes = abstract_cjk_sizes(pdf.read_bytes(), mono)
    # The Chinese abstract is in the box (it was not kept in English).
    assert sum(sizes.values()) > 400, sizes
    # The source is 10.1 pt; the abstract was set at 9.0 pt with a 1.5 line skip.
    assert min(sizes) >= 9.5, sizes


def char_units(text: str, widths: dict[str, float]) -> list[TypesettingUnit]:
    """One typesetting unit per character, 10 pt high, at the given widths."""
    return [
        TypesettingUnit(
            char=PdfCharacter(
                char_unicode=c,
                box=Box(x=0, y=0, x2=widths[c], y2=10),
                pdf_style=PdfStyle(font_id="F", font_size=10),
            )
        )
        for c in text
    ]


def bare_typesetting() -> Typesetting:
    """A Typesetting without a translation config: enough for the layout
    methods, whose only other dependency is the base font's CJK width."""
    typesetting = object.__new__(Typesetting)
    typesetting.font_mapper = SimpleNamespace(
        base_font=SimpleNamespace(char_lengths=lambda text, size: [size])
    )
    typesetting.is_cjk = True
    return typesetting


def test_an_english_word_that_fits_is_not_broken_before_a_wider_letter():
    # AutoRef page 9 at the 1.25 line skip: "Seedream" fit at the end of a line
    # but came out as "Seedrea" / "m". The English line break check added the
    # width up to the next break point, which already holds the current letter,
    # to the current letter's width: the word passed at its narrow "S" and
    # failed at its wider "m".
    widths = {"中": 10, "文": 10, " ": 3, "S": 5, "e": 4, "d": 5, "r": 3, "a": 4, "m": 8}
    units = char_units("中文中文 Seedream 中文", widths)
    # "中文中文 " ends at 43, "Seedream" (37 pt) at 80: the word fits a line
    # ending at 86, but 72 (before "m") + 8 + 8 does not.
    laid_out, fits = bare_typesetting()._layout_typesetting_units(
        units,
        Box(x=0, y=0, x2=86, y2=100),
        1.0,
        1.25,
        SimpleNamespace(first_line_indent=False, hanging_indent=None),
    )

    assert fits
    lines = {}
    for unit in laid_out:
        lines.setdefault(round(unit.box.y, 3), []).append(unit.try_get_unicode())
    first_line = next(iter(lines.values()))
    assert "".join(first_line).rstrip() == "中文中文 Seedream"


def test_a_paragraph_that_does_not_fit_at_the_scale_floor_is_set_there_and_logged(caplog):
    # Ten 10 pt CJK characters in a 20 x 12 pt box: at the floor (0.5) they
    # take 3 lines of 4 and the third runs below the box. The paragraph is set
    # at the floor anyway (it used to shrink to 0.1) and a warning names it
    # and how far it runs past its box.
    units = char_units("中文" * 5, {"中": 10, "文": 10})
    paragraph = SimpleNamespace(
        box=Box(x=0, y=0, x2=20, y2=12),
        debug_id="floor-p",
        first_line_indent=False,
        hanging_indent=None,
        scale=None,
        pdf_paragraph_composition=[],
    )
    # No page content: the attempts to grow the box into free space fail.
    page = SimpleNamespace(pdf_curve=[], pdf_form=[])

    with caplog.at_level(logging.WARNING, logger=typesetting_module.__name__):
        scale, laid_out = bare_typesetting()._find_optimal_scale_and_layout(
            paragraph, page, units, apply_layout=True
        )

    assert scale == pytest.approx(MIN_SCALE)
    assert paragraph.scale == pytest.approx(MIN_SCALE)
    assert len(paragraph.pdf_paragraph_composition) == 10
    lowest = min(unit.box.y for unit in laid_out)
    assert lowest < 0
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("floor-p" in w and f"{-lowest:.1f} pt" in w for w in warnings), warnings
