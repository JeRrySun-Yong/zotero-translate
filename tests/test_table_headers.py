"""Chinese table headers are not shrunk far below the source size (closure ticket 06, T4).

The header cells of MaLiang page 9 table 2 are set at 6.05 pt in narrow
cells: "Calls" is 12 pt wide, "(task-h)" 18 pt. Their Chinese ("调用次数",
"（任务·小时）") is wider than the English, and the engine shrank it until it
fitted the source cell: 3.0 pt and 3.6 pt (C-05). The cells have free space on
both sides within their table row, which a table cell may use.
"""

import json
import unicodedata
from types import SimpleNamespace

import pymupdf
import pytest
from babeldoc.format.pdf.document_il import Box, PdfStyle

from tests.conftest import FIXTURES
from tests.fake_model import batch_items
from tests.test_line_spacing import bare_typesetting, char_units

EXCERPT = FIXTURES / "maliang-p9.pdf"
# The real model's Chinese for the header of table 2 (C-05 mono.pdf).
CHINESE = {
    "Task Completion": "任务完成情况",
    "Generation Time": "生成时间",
    "Model Usage": "模型用量",
    "Model": "模型",
    "Success": "成功率",
    "Failures": "失败数",
    "Total Time": "总时间",
    "Time/Task": "单任务时间",
    "Calls": "调用次数",
    "Input Tokens": "输入词元",
    "Output Tokens": "输出词元",
    "(task-h)": "（任务·小时）",
    "(min/image)": "（分钟/图像）",
    "(min/case)": "（分钟/例）",
    "(calls/case)": "（次/例）",
    "(k/case)": "（k/例）",
    "(cases)": "（例）",
}
# The two header rows of table 2 (PDF points from the top) and their source size.
HEADER = pymupdf.Rect(100, 128, 510, 148)
SOURCE_SIZE = 6.05
# Ticket 06: a Chinese header character is at least 0.8 of its cell's source size.
MIN_SHARE = 0.8


def header_answer(body: dict) -> str:
    answer = []
    for item in batch_items(body["messages"][-1]["content"]) or []:
        text = item["input"]
        for english in sorted(CHINESE, key=len, reverse=True):
            text = text.replace(english, CHINESE[english])
        answer.append({"id": item["id"], "output": text})
    return json.dumps(answer, ensure_ascii=False)


def header_glyphs(pdf: bytes) -> list[tuple[str, float, pymupdf.Rect]]:
    """The non-blank characters of the header rows: character (NFKC), span
    size, glyph box as high as the font size."""
    glyphs = []
    small = pymupdf.TOOLS.set_small_glyph_heights()
    pymupdf.TOOLS.set_small_glyph_heights(True)
    try:
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            raw = doc[0].get_text("rawdict", clip=HEADER)
    finally:
        pymupdf.TOOLS.set_small_glyph_heights(small)
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    if char["c"].strip():
                        glyphs.append((unicodedata.normalize("NFKC", char["c"]), span["size"], pymupdf.Rect(char["bbox"])))
    return glyphs


def test_narrow_header_cells_keep_their_chinese_near_the_source_size(fake_model, start_service):
    fake_model.respond("translate_batch", header_answer)
    service = start_service(fake_model)
    task_id = service.submit(EXCERPT)
    final = service.wait(task_id)
    assert final["state"] == "succeeded", final["error"]

    glyphs = header_glyphs(service.download(task_id, "mono"))
    text = "".join(c for c, *_ in glyphs)
    for header in ("调用次数", "任务", "小时", "单任务时间"):
        assert header in text, text
    small = sorted({(c, round(size, 2)) for c, size, _ in glyphs if "一" <= c <= "鿿" and size < MIN_SHARE * SOURCE_SIZE})
    assert small == [], small
    # The widened Chinese cells do not run into other text (the English
    # glyphs of one word may touch: "fi" in "Qualified").
    touching = [
        (a, b)
        for i, (a, _, box_a) in enumerate(glyphs)
        for b, _, box_b in glyphs[i + 1 :]
        if ("一" <= a <= "鿿" or "一" <= b <= "鿿")
        and (box_a & box_b).width > 0.5
        and (box_a & box_b).height > 0.5
    ]
    assert touching == [], touching


# Synthetic table cells (PDF coordinates, y up): a 20 x 12 pt cell at x 50-70
# holding four 10 pt CJK characters (40 pt), in a table 200 pt wide. Engine
# internals, as in tests/test_line_spacing.py: the excerpts have no
# left-aligned cell that needs to widen.
CELL = Box(x=50, y=100, x2=70, y2=112)
TABLE = Box(x=0, y=0, x2=200, y2=200)


def lay_out_cell(column: list[Box], shapes: dict | None = None) -> tuple[float, list, SimpleNamespace]:
    """Lay out the cell with the other cells of its column (other rows) and
    optional page shapes ({"pdf_curve": [...], ...}); returns the scale, the
    laid-out units and the paragraph."""
    paragraph = SimpleNamespace(
        box=CELL,
        debug_id="cell",
        first_line_indent=False,
        hanging_indent=None,
        scale=None,
        vertical=False,
        pdf_style=PdfStyle(font_id="F", font_size=10),
        pdf_paragraph_composition=[],
    )
    page = SimpleNamespace(
        page_layout=[SimpleNamespace(class_name="table", box=TABLE)],
        pdf_paragraph=[paragraph] + [SimpleNamespace(box=box) for box in column],
        pdf_character=[],
        pdf_figure=[],
        pdf_form=[],
        pdf_curve=[],
    )
    for name, boxes in (shapes or {}).items():
        setattr(page, name, [SimpleNamespace(box=box) for box in boxes])
    units = char_units("中文" * 2, {"中": 10, "文": 10})
    scale, laid_out = bare_typesetting()._find_optimal_scale_and_layout(paragraph, page, units, apply_layout=True)
    return scale, laid_out, paragraph


def test_a_left_aligned_cell_widens_to_the_right_and_keeps_its_left_edge():
    # Review of ticket 06: centring every widened cell moved left-aligned
    # text cells left (AutoDesign page 25 by 13.4 pt) and lost the indent of
    # a continuation line (page 22). Two cells of the column start at x 50.
    scale, laid_out, paragraph = lay_out_cell([Box(x=50, y=80, x2=90, y2=92), Box(x=50, y=60, x2=75, y2=72)])

    assert scale == pytest.approx(1.0)
    assert min(unit.box.x for unit in laid_out) == pytest.approx(CELL.x)
    assert paragraph.box.x == pytest.approx(CELL.x)


def test_a_centred_cell_widens_to_both_sides_and_stays_centred():
    # Two cells of the column share the cell's centre (x 60), as a header
    # over its column of numbers.
    scale, laid_out, _ = lay_out_cell([Box(x=45, y=80, x2=75, y2=92), Box(x=52, y=60, x2=68, y2=72)])

    assert scale == pytest.approx(1.0)
    left, right = min(u.box.x for u in laid_out), max(u.box.x2 for u in laid_out)
    assert (left + right) / 2 == pytest.approx(60, abs=0.5)


def test_a_table_rule_on_the_row_stops_the_widening():
    # A vertical rule (a curve) 2 pt right of the centred cell: the cell
    # cannot widen past it and shrinks to fit its own box instead.
    rule = Box(x=72, y=0, x2=72.5, y2=200)
    scale, laid_out, _ = lay_out_cell(
        [Box(x=45, y=80, x2=75, y2=92), Box(x=52, y=60, x2=68, y2=72)], {"pdf_curve": [rule]}
    )

    assert max(unit.box.x2 for unit in laid_out) <= rule.x
    assert scale < 1.0
